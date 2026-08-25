import base64
import json
import sqlite3
import hashlib
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import (
    short_drama,
    short_drama_asset_graph,
    short_drama_conversation,
    short_drama_storyboard,
)


class Handler:
    def __init__(self, path, token="alice", body=None, idempotency_key="test-key-123"):
        self.path = path
        self.token = token
        self.body = body
        self.headers = {"Idempotency-Key": idempotency_key}
        self.response = None

    def _token(self):
        return self.token

    def _json_body_strict(self):
        return self.body

    def _send(self, status, payload):
        self.response = (status, payload)


def payload(**changes):
    value = {
        "title": "雨夜来信",
        "synopsis": "两位旧友在雨夜重逢，并发现当年的误会另有隐情。",
        "ratio": "16:9",
        "target_duration": 30,
        "shot_count": 6,
        "genre": "悬疑推理",
        "visual_style": "电影感写实",
        "point_budget": 0,
    }
    value.update(changes)
    return value


def confirmed_contract():
    shots = []
    beats = []
    for index in range(1, 7):
        shots.append({
            "index": index,
            "phase": "阶段%d" % index,
            "duration": 5,
            "scene": "确认场景%d" % index,
            "characters": ["林夏", "周野"],
            "action": "确认动作%d" % index,
            "expression": "确认表情%d" % index,
            "speaker": "林夏" if index == 1 else "",
            "dialogue_kind": "dialogue" if index == 1 else "silence",
            "dialogue": "这是确认台词" if index == 1 else "",
            "camera": "确认镜头%d" % index,
            "sound": "确认声音%d" % index,
            "transition": "确认转场%d" % index,
            "continuity": "确认连续性%d" % index,
            "summary": "确认摘要%d" % index,
            "locked": index == 2,
        })
        beats.append({
            "index": index,
            "phase": "阶段%d" % index,
            "summary": "确认摘要%d" % index,
            "duration": 5,
        })
    return {
        "schema_version": "preproject-confirmed-shot-contract-v1",
        "title": "确认短剧",
        "logline": "两位旧友在雨夜重逢并化解误会。",
        "protagonist": "林夏",
        "conflict": "是否相信旧友",
        "ending": "两人完成和解",
        "ratio": "16:9",
        "duration_seconds": 30,
        "shot_count": 6,
        "genre": "悬疑推理",
        "visual_style": "电影感写实",
        "characters": ["林夏", "周野"],
        "beats": beats,
        "shots": shots,
    }


class ShortDramaSourceAnchorTests(unittest.TestCase):
    def test_source_anchors_group_explicit_shots_without_repeating_full_script(self):
        source = (
            "人物：小晚、阿泽。分镜、时长、画面依次安排："
            "1、0-5秒：少女在街角与少年相撞。"
            "2、5-10秒：两人俯身捡起书签。"
            "3、10-15秒：少年递还书签，两人短暂交谈。"
            "4、15-20秒：少女发现两人喜欢同一本书。"
            "5、20-25秒：两人在路口温柔道别。"
            "6、25-30秒：街灯亮起，两人各自离开。"
        )
        anchors = short_drama_conversation._source_anchors(source)
        self.assertEqual(["start", "middle", "end"], [item["position"] for item in anchors])
        self.assertIn("少女在街角", anchors[0]["excerpt"])
        self.assertIn("喜欢同一本书", anchors[1]["excerpt"])
        self.assertIn("街灯亮起", anchors[2]["excerpt"])
        self.assertNotIn("人物：小晚", anchors[0]["excerpt"])
        self.assertTrue(all(len(item["excerpt"]) <= 221 for item in anchors))


class ShortDramaDialogueTimingTests(unittest.TestCase):
    def test_duplicate_dialogue_text_can_play_simultaneously(self):
        characters = [{
            "character_key": "lead", "name": "林小雨", "role_type": "main",
            "identity": "姐姐", "personality": "好胜",
        }, {
            "character_key": "brother", "name": "林豆", "role_type": "main",
            "identity": "弟弟", "personality": "机灵",
        }]
        script = short_drama_storyboard.compile_storyboard(
            payload(shot_count=3, target_duration=15),
            ["发现饼干", "争抢", "和解"], characters,
        )
        shot = script["shots"][0]
        short_drama_conversation._apply_shot_patch(script, shot["shot_key"], {
            "dialogues": [{
                "kind": "dialogue", "character_key": "lead",
                "text": "这块饼干是我的呀", "speech_rate": 1.0,
                "timing_mode": "sequential",
            }, {
                "kind": "dialogue", "character_key": "brother",
                "text": "这块饼干是我的呀", "speech_rate": 1.0,
                "timing_mode": "simultaneous",
            }],
        })
        line_by_id = {item["id"]: item for item in script["dialogue_lines"]}
        lines = [line_by_id[line_id] for line_id in shot["dialogue_line_ids"]]
        self.assertEqual(
            ["sequential", "simultaneous"],
            [item["timing_mode"] for item in lines],
        )
        self.assertEqual([lines[0]["text"], lines[0]["text"]], [item["text"] for item in lines])
        self.assertEqual("pass", short_drama_storyboard.validate_script(script)["status"])

        short_drama_conversation._structure_shot(script, shot["shot_key"], "copy")
        copied = script["shots"][1]
        line_by_id = {item["id"]: item for item in script["dialogue_lines"]}
        self.assertEqual(
            ["sequential", "simultaneous"],
            [line_by_id[line_id]["timing_mode"] for line_id in copied["dialogue_line_ids"]],
        )
        regenerated_lines = [{"text": "新的第一句"}, {"text": "新的第二句"}]
        short_drama_conversation._preserve_dialogue_timing_modes(
            lines, regenerated_lines
        )
        self.assertEqual(
            ["sequential", "simultaneous"],
            [item["timing_mode"] for item in regenerated_lines],
        )

    def test_duration_only_patch_cannot_bypass_aggregate_dialogue_timing(self):
        characters = [{
            "character_key": "lead", "name": "林夏", "role_type": "main",
            "identity": "主角", "personality": "坚定",
        }, {
            "character_key": "friend", "name": "周野", "role_type": "support",
            "identity": "旧友", "personality": "克制",
        }]
        script = short_drama_storyboard.compile_storyboard(
            payload(shot_count=3, target_duration=15),
            ["相遇", "冲突", "和解"], characters,
        )
        shot = script["shots"][0]
        short_drama_conversation._apply_shot_patch(
            script, shot["shot_key"], {"duration_seconds": 6}
        )
        short_drama_conversation._apply_shot_patch(script, shot["shot_key"], {
            "dialogues": [{
                "kind": "dialogue", "character_key": "lead",
                "text": "一二三四五六七", "speech_rate": 1.0,
            }, {
                "kind": "dialogue", "character_key": "friend",
                "text": "一二三四五六七", "speech_rate": 1.0,
            }],
        })

        with self.assertRaises(short_drama_conversation.ConversationError) as raised:
            short_drama_conversation._apply_shot_patch(
                script, shot["shot_key"], {"duration_seconds": 4}
            )
        self.assertEqual("dialogue_too_long", raised.exception.code)

    def test_multi_speaker_lines_are_validated_and_copied_in_order(self):
        characters = [{
            "character_key": "lead", "name": "林夏", "role_type": "main",
            "identity": "主角", "personality": "坚定",
        }, {
            "character_key": "friend", "name": "周野", "role_type": "support",
            "identity": "旧友", "personality": "克制",
        }]
        script = short_drama_storyboard.compile_storyboard(
            payload(shot_count=3, target_duration=15),
            ["相遇", "冲突", "和解"], characters,
        )
        shot = script["shots"][0]
        shot["sound_design"] = "客厅安静环境声，拿起饼干时加入包装摩擦声。"
        short_drama_conversation._apply_shot_patch(script, shot["shot_key"], {
            "dialogues": [{
                "kind": "dialogue", "character_key": "lead",
                "text": "来了。", "speech_rate": 1.0,
            }, {
                "kind": "dialogue", "character_key": "friend",
                "text": "刚到。", "speech_rate": 1.15,
            }],
        })
        line_by_id = {item["id"]: item for item in script["dialogue_lines"]}
        self.assertEqual(2, len(shot["dialogue_line_ids"]))
        self.assertEqual(
            ["林夏", "周野"],
            [line_by_id[line_id]["speaker"] for line_id in shot["dialogue_line_ids"]],
        )
        self.assertEqual("pass", short_drama_storyboard.validate_script(script)["status"])

        original_ids = list(shot["dialogue_line_ids"])
        short_drama_conversation._structure_shot(script, shot["shot_key"], "copy")
        copied = script["shots"][1]
        self.assertEqual(2, len(copied["dialogue_line_ids"]))
        self.assertEqual(shot["sound_design"], copied["sound_design"])
        self.assertTrue(set(original_ids).isdisjoint(copied["dialogue_line_ids"]))
        line_by_id = {item["id"]: item for item in script["dialogue_lines"]}
        self.assertEqual(
            ["来了。", "刚到。"],
            [line_by_id[line_id]["text"] for line_id in copied["dialogue_line_ids"]],
        )

        with self.assertRaises(short_drama_conversation.ConversationError) as raised:
            short_drama_conversation._apply_shot_patch(script, shot["shot_key"], {
                "dialogues": [{
                    "kind": "dialogue", "character_key": "lead",
                    "text": "第%d句" % index, "speech_rate": 2.0,
                } for index in range(7)],
            })
        self.assertEqual("dialogue_count_invalid", raised.exception.code)

    def test_overridden_dialogue_is_rebalanced_or_condensed_before_validation(self):
        def sample(durations, text):
            shots = []
            lines = []
            for index, duration in enumerate(durations):
                line_id = "line_%d" % index
                lines.append({
                    "id": line_id,
                    "kind": "dialogue" if index == 0 else "silence",
                    "character_key": "lead" if index == 0 else "",
                    "speaker": "Lead" if index == 0 else "",
                    "text": text if index == 0 else "",
                })
                shots.append({
                    "shot_key": "shot_%d" % index,
                    "duration_seconds": duration,
                    "purpose": "purpose_%d" % index,
                    "visual": "visual_%d" % index,
                    "provider_prompt": "prompt_%d" % index,
                    "dialogue_line_ids": [line_id],
                })
            return {
                "overview": {"duration_seconds": sum(durations)},
                "characters": [{"character_key": "lead", "name": "Lead"}],
                "dialogue_lines": lines,
                "shots": shots,
            }

        rebalanced = sample([5, 5, 5], "abcdefghijklmnop")
        adjustments = short_drama_conversation.short_drama_storyboard.normalize_dialogue_timing(
            rebalanced
        )
        self.assertEqual(15, sum(item["duration_seconds"] for item in rebalanced["shots"]))
        self.assertEqual([5, 5, 5], [item["duration_seconds"] for item in rebalanced["shots"]])
        self.assertNotEqual("abcdefghijklmnop", rebalanced["dialogue_lines"][0]["text"])
        self.assertTrue(any(item["kind"] == "dialogue_condensed" for item in adjustments))
        self.assertFalse(any(
            item["code"] == "dialogue_too_long"
            for item in short_drama_conversation.short_drama_storyboard.analyze_quality(rebalanced)["blockers"]
        ))

        condensed = sample([4, 4], "abcdefghijklmnopqrstuvwxyz")
        short_drama_conversation.short_drama_storyboard.normalize_dialogue_timing(condensed)
        self.assertEqual(
            "abcdefghijklmnopqrstuvwxyz",
            condensed["dialogue_lines"][0]["original_text"],
        )
        self.assertTrue(condensed["dialogue_lines"][0]["auto_fitted_to_duration"])
        self.assertFalse(any(
            item["code"] == "dialogue_too_long"
            for item in short_drama_conversation.short_drama_storyboard.analyze_quality(condensed)["blockers"]
        ))

        provider_ready = sample([6, 3, 3, 4, 6, 8], "")
        short_drama_conversation.short_drama_storyboard.normalize_dialogue_timing(provider_ready)
        provider_durations = [item["duration_seconds"] for item in provider_ready["shots"]]
        self.assertEqual(30, sum(provider_durations))
        self.assertTrue(all(4 <= value <= 15 for value in provider_durations))

    def test_structure_actions_keep_user_selected_shot_count_and_duration(self):
        script = short_drama_conversation.short_drama_storyboard.compile_storyboard(
            payload(shot_count=3, target_duration=15),
            ["相遇", "产生误会", "和解"],
            [{
                "character_key": "lead", "name": "林夏", "role_type": "main",
                "identity": "主角", "personality": "坚定",
            }],
        )
        original_keys = [item["shot_key"] for item in script["shots"]]
        short_drama_conversation._structure_shot(
            script, original_keys[0], "smart_insert", "补一个自然过渡镜头",
        )
        self.assertEqual(4, len(script["shots"]))
        self.assertEqual(4, script["shot_planning"]["shot_count"])
        self.assertEqual(
            sum(item["duration_seconds"] for item in script["shots"]),
            script["overview"]["duration_seconds"],
        )
        inserted_key = script["shots"][1]["shot_key"]
        self.assertTrue(inserted_key.startswith("shot_user_"))
        short_drama_conversation._structure_shot(script, inserted_key, "delete")
        self.assertEqual(original_keys, [item["shot_key"] for item in script["shots"]])

    def test_locked_shots_and_locked_adjacency_block_structure_actions(self):
        script = short_drama_conversation.short_drama_storyboard.compile_storyboard(
            payload(shot_count=3, target_duration=15),
            ["start", "middle", "end"],
            [{
                "character_key": "lead", "name": "Lead", "role_type": "main",
                "identity": "lead", "personality": "steady",
            }],
        )
        keys = [item["shot_key"] for item in script["shots"]]
        script["shots"][1]["locked"] = True

        for action in (
            "delete", "copy", "move_up", "move_down",
            "insert_before", "insert_after", "smart_insert",
        ):
            with self.subTest(action=action):
                try:
                    short_drama_conversation._structure_shot(
                        json.loads(json.dumps(script)), keys[1], action, "bridge",
                    )
                except short_drama_conversation.ConversationError as error:
                    self.assertEqual("shot_locked", error.code)
                    self.assertEqual(409, error.status)
                else:
                    self.fail("locked shot accepted %s" % action)

        adjacent_cases = (
            (keys[2], "move_up"),
            (keys[0], "move_down"),
            (keys[2], "insert_before"),
            (keys[0], "insert_after"),
            (keys[0], "smart_insert"),
            (keys[0], "copy"),
            (keys[0], "delete"),
            (keys[2], "delete"),
        )
        for shot_key, action in adjacent_cases:
            with self.subTest(shot_key=shot_key, action=action):
                try:
                    short_drama_conversation._structure_shot(
                        json.loads(json.dumps(script)), shot_key, action, "bridge",
                    )
                except short_drama_conversation.ConversationError as error:
                    self.assertEqual("shot_locked", error.code)
                    self.assertEqual(409, error.status)
                else:
                    self.fail("locked adjacency accepted %s" % action)

        move_script = short_drama_conversation.short_drama_storyboard.compile_storyboard(
            payload(shot_count=4, target_duration=30),
            ["one", "two", "three", "four"],
            [{
                "character_key": "lead", "name": "Lead", "role_type": "main",
                "identity": "lead", "personality": "steady",
            }],
        )
        move_keys = [item["shot_key"] for item in move_script["shots"]]
        outer_cases = (
            (0, move_keys[1], "move_down"),
            (0, move_keys[2], "move_up"),
            (3, move_keys[1], "move_down"),
            (3, move_keys[2], "move_up"),
        )
        for locked_index, shot_key, action in outer_cases:
            candidate = json.loads(json.dumps(move_script))
            candidate["shots"][locked_index]["locked"] = True
            with self.subTest(
                locked_index=locked_index, shot_key=shot_key, action=action,
            ):
                with self.assertRaises(
                    short_drama_conversation.ConversationError,
                ) as blocked:
                    short_drama_conversation._structure_shot(
                        candidate, shot_key, action, "bridge",
                    )
                self.assertEqual("shot_locked", blocked.exception.code)
                self.assertEqual(409, blocked.exception.status)

    def test_duration_edit_changes_total_instead_of_truncating_other_shots(self):
        script = short_drama_conversation.short_drama_storyboard.compile_storyboard(
            payload(shot_count=3, target_duration=15),
            ["相遇", "冲突", "和解"],
            [{
                "character_key": "lead", "name": "林夏", "role_type": "main",
                "identity": "主角", "personality": "坚定",
            }],
        )
        durations = [item["duration_seconds"] for item in script["shots"]]
        requested = min(15, durations[0] + 2)
        short_drama_conversation._rebalance_duration(
            script, script["shots"][0], requested,
        )
        self.assertEqual(durations[1:], [item["duration_seconds"] for item in script["shots"]][1:])
        self.assertEqual(sum(durations) + requested - durations[0], script["overview"]["duration_seconds"])


class ShortDramaStoryboardQualityTests(unittest.TestCase):
    def _compile_single_fact(self, shot_count):
        return short_drama_storyboard.compile_storyboard(
            payload(shot_count=shot_count, target_duration=60),
            ["主持人在舞台上面对突发质疑，必须作出回应"],
            [{
                "character_key": "host", "name": "主持人",
                "role_type": "main", "identity": "主持人",
                "personality": "沉着",
            }],
        )

    def test_repeated_story_phases_compile_distinct_visuals_before_generation(self):
        script = self._compile_single_fact(10)

        self.assertNotEqual("blocked", script["quality_gate"]["status"])
        self.assertFalse(script["quality_gate"]["blockers"])
        visuals = [shot["visual"] for shot in script["shots"]]
        self.assertEqual(len(visuals), len(set(visuals)))
        self.assertTrue(all(shot["provider_prompt"] for shot in script["shots"]))

    def test_fifteen_shot_boundary_keeps_each_visual_distinct(self):
        script = self._compile_single_fact(15)

        self.assertNotEqual("blocked", script["quality_gate"]["status"])
        self.assertFalse(script["quality_gate"]["blockers"])
        visuals = [shot["visual"] for shot in script["shots"]]
        self.assertEqual(15, len(set(visuals)))

    def test_four_plus_phase_occurrences_use_visible_state_transitions(self):
        progressions = [
            short_drama_storyboard._phase_progression("change", occurrence, 5)
            for occurrence in range(1, 6)
        ]

        self.assertEqual([
            "先展示变化前的状态",
            "人物伸手触碰关键物件，变化动作刚刚启动",
            "关键物件被移动或打开，人物位置随之改变",
            "人物完成推动动作，表情与双方距离形成新的可见状态",
            "最后呈现变化后的表情与关系",
        ], progressions)

        script = self._compile_single_fact(17)
        transition_visuals = [
            shot["visual"] for shot in script["shots"]
            if "人物伸手触碰关键物件" in shot["visual"]
            or "人物完成推动动作" in shot["visual"]
        ]
        self.assertFalse(script["quality_gate"]["blockers"])
        self.assertEqual(2, len(transition_visuals))
        self.assertTrue(any(
            "人物伸手触碰关键物件" in visual
            for visual in transition_visuals
        ))
        self.assertTrue(any(
            "人物完成推动动作" in visual
            for visual in transition_visuals
        ))
        provider_prompts = [
            shot["provider_prompt"] for shot in script["shots"]
            if shot["visual"] in transition_visuals
        ]
        self.assertTrue(any(
            "人物伸手触碰关键物件" in prompt
            for prompt in provider_prompts
        ))
        self.assertTrue(any(
            "人物完成推动动作" in prompt
            for prompt in provider_prompts
        ))

    def test_quality_gate_still_rejects_genuinely_identical_visuals(self):
        script = self._compile_single_fact(6)
        script["shots"][1]["visual"] = script["shots"][0]["visual"]

        quality = short_drama_storyboard.analyze_quality(script)

        self.assertIn(
            "duplicate_visual",
            [item["code"] for item in quality["blockers"]],
        )


class ShortDramaConversationTests(unittest.TestCase):
    def test_live_action_script_keeps_confirmed_role_keys_and_ignores_incidental_friends(self):
        role_contract = [{
            "character_key": "boy_role", "name": "男孩",
            "identity_text": "故事中的男孩", "personality": "内向",
            "role_type": "main",
        }, {
            "character_key": "girl_role", "name": "女孩",
            "identity_text": "故事中的女孩", "personality": "开朗",
            "role_type": "support",
        }]
        source_import = {
            "source_text": "男孩把糖果分给女孩，两个小朋友成为了朋友。",
            "source_hash": "role-contract-source",
            "import_mode": "faithful", "content_type": "live_action",
            "character_contract": role_contract,
        }
        understanding = {
            "import_contract": short_drama_conversation._import_contract(source_import),
            "ending": "两个小朋友成为朋友",
        }
        script = short_drama_conversation._script(
            payload(title="分享糖果", synopsis=source_import["source_text"]),
            [], understanding=understanding, source_import=source_import,
        )
        self.assertEqual(
            [("boy_role", "男孩"), ("girl_role", "女孩")],
            [(item["character_key"], item["name"]) for item in script["characters"]],
        )
        self.assertTrue(all(
            item["source_type"] == "system_generated"
            for item in script["shots"]
        ))
        self.assertEqual(
            ["main", "support"],
            [item["role_type"] for item in script["characters"]],
        )

    def test_automatic_storyboard_does_not_promote_unmentioned_crowd(self):
        characters = [{
            "character_key": "crowd", "name": "路人", "role_type": "crowd",
            "identity": "背景路人", "personality": "",
        }, {
            "character_key": "lead", "name": "林夏", "role_type": "main",
            "identity": "主角", "personality": "坚定",
        }, {
            "character_key": "support", "name": "周野", "role_type": "support",
            "identity": "朋友", "personality": "克制",
        }]
        script = short_drama_conversation.short_drama_storyboard.compile_storyboard(
            payload(shot_count=2, target_duration=8), ["故事开始", "两人和解"],
            characters,
        )
        self.assertEqual(["lead"], script["shots"][0]["character_keys"])
        self.assertEqual(
            ["lead", "support"], script["shots"][1]["character_keys"],
        )
        self.assertIn("林夏（主要角色）", script["shots"][0]["provider_prompt"])
        self.assertNotIn("路人", script["shots"][0]["provider_prompt"])

    def test_explicit_storyboard_in_source_overrides_generated_shots(self):
        role_contract = [{
            "character_key": "boy_role", "name": "男孩",
            "identity_text": "故事中的男孩", "personality": "内向",
        }, {
            "character_key": "girl_role", "name": "女孩",
            "identity_text": "故事中的女孩", "personality": "开朗",
        }]
        source = "\n".join([
            "人物：男孩、女孩",
            "镜头 1（0-4s）近景，男孩把糖果递给女孩。",
            "镜头 2（4-10s）全景，女孩接过糖果。字幕：好东西一起分享。",
        ])
        source_import = {
            "source_text": source, "source_hash": "explicit-shot-source",
            "import_mode": "faithful", "content_type": "live_action",
            "character_contract": role_contract,
        }
        script = short_drama_conversation._script(
            payload(title="分享糖果", synopsis=source, target_duration=10, shot_count=2),
            [],
            understanding={
                "import_contract": short_drama_conversation._import_contract(source_import),
                "ending": "两个孩子分享糖果",
            },
            source_import=source_import,
        )
        self.assertEqual([4, 6], [item["duration_seconds"] for item in script["shots"]])
        self.assertEqual(
            ["user_storyboard", "user_storyboard"],
            [item["source_type"] for item in script["shots"]],
        )
        self.assertEqual("近景", script["shots"][0]["camera"])
        self.assertIn("男孩把糖果递给女孩", script["shots"][0]["visual"])
        self.assertEqual("on_screen_text", script["dialogue_lines"][1]["kind"])
        self.assertEqual("好东西一起分享", script["dialogue_lines"][1]["text"])
        self.assertEqual(2, script["storyboard_source"]["explicit_shot_count"])
        self.assertEqual(0, script["storyboard_source"]["generated_shot_count"])

    def test_explicit_storyboard_rejects_duration_conflict(self):
        source = "镜头 1（0-8s）近景，人物出现。\n镜头 2（8-16s）全景，人物离开。"
        source_import = {
            "source_text": source, "source_hash": "duration-conflict",
            "import_mode": "faithful", "content_type": "live_action",
        }
        with self.assertRaises(short_drama_conversation.ConversationError) as raised:
            short_drama_conversation._script(
                payload(synopsis=source, target_duration=10, shot_count=2),
                [],
                understanding={
                    "import_contract": short_drama_conversation._import_contract(source_import),
                },
                source_import=source_import,
            )
        self.assertEqual("explicit_storyboard_duration_mismatch", raised.exception.code)

    def test_long_import_builds_global_structure_from_start_to_end(self):
        source = "\n".join([
            "第一场 家中", "林夏：我必须找到父亲。", "林夏带着旧信离开。",
            "第二场 车站", "周野阻止林夏登车。", "林夏发现信件背后的真相。",
            "第三场 月台", "林夏作出选择。", "父女最终和解。",
        ])
        structure = short_drama_conversation._import_global_structure(
            source, ["林夏", "周野"],
        )
        self.assertEqual("short-drama-import-global-v1", structure["schema_version"])
        self.assertTrue(structure["coverage"]["analyzed_from_start"])
        self.assertTrue(structure["coverage"]["analyzed_from_end"])
        self.assertIn("和解", structure["ending"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name) / "content.db")
        self.db = lambda: sqlite3.connect(self.database)
        short_drama.init_db(self.db)
        self.project = short_drama.create_project(self.db, "alice", payload())

    def tearDown(self):
        self.tmp.cleanup()

    def confirm_direction(self, project_id, revision=1, key_prefix="confirm"):
        selected = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": project_id,
                "conversation_revision": revision,
                "message": "方案一 · 情感治愈",
            },
            key_prefix + "-select",
        )
        return short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": project_id,
                "conversation_revision": selected["conversation"]["revision"],
                "message": "确认这个方向",
            },
            key_prefix + "-confirm",
        )

    def set_and_lock_scene_reference(self, workspace, scene_key, prompt):
        raw = b"\x89PNG\r\n\x1a\n" + prompt.encode("utf-8")
        data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        output = Path(self.tmp.name) / "output"
        fake_image = types.SimpleNamespace(OUT_DIR=output)
        fake_uploads = types.SimpleNamespace(
            MAX_BYTES=10 * 1024 * 1024,
            MIME_EXTENSIONS={"image/png": ".png"},
            detect_mime=lambda value: "image/png" if value.startswith(b"\x89PNG") else "",
        )
        package = short_drama_asset_graph.__package__
        with mock.patch.dict(sys.modules, {
            package + ".image": fake_image,
            package + ".cli_uploads": fake_uploads,
        }), mock.patch.object(
            sys.modules[package], "image", fake_image, create=True,
        ), mock.patch.object(
            sys.modules[package], "cli_uploads", fake_uploads, create=True,
        ):
            created = short_drama_asset_graph.set_scene_reference(
                self.db, "alice", "alice", {
                    "project_id": self.project["id"],
                    "graph_revision": workspace["graph_revision"],
                    "scene_key": scene_key, "source": "upload",
                    "image_data": data_url, "filename": "scene.png", "prompt": prompt,
                },
            )
        return short_drama_asset_graph.lock_scene_reference(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "graph_revision": created["graph_revision"],
                "scene_key": scene_key,
            },
        )

    def locked_scene_reference(self, shot_key):
        with closing(self.db()) as conn:
            conn.row_factory = sqlite3.Row
            return short_drama_asset_graph.locked_scene_reference(
                conn, self.project["id"], shot_key,
            )

    def test_workspace_is_free_and_starts_without_script(self):
        result = short_drama_conversation.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual("idea_intake", result["conversation"]["state"])
        self.assertEqual([], result["messages"])
        self.assertIsNone(result["current_script"])
        self.assertEqual({"cost": 0, "charged": False}, result["billing"])

    def test_confirmed_preproject_contract_is_persisted_before_explicit_lock(self):
        confirmed = self.confirm_direction(self.project["id"], 1, "contract")
        shots = []
        beats = []
        for index in range(1, 7):
            shots.append({
                "index": index,
                "phase": "阶段%d" % index,
                "duration": 5,
                "scene": "确认场景%d" % index,
                "characters": ["林夏", "周野"],
                "action": "CONFIRMED_ACTION_%d" % index,
                "expression": "CONFIRMED_EXPRESSION_%d" % index,
                "speaker": "林夏" if index == 1 else "",
                "dialogue_kind": "dialogue" if index == 1 else "silence",
                "dialogue": "这是确认台词" if index == 1 else "",
                "camera": "CONFIRMED_CAMERA_%d" % index,
                "sound": "CONFIRMED_SOUND_%d" % index,
                "transition": "CONFIRMED_TRANSITION_%d" % index,
                "continuity": "CONFIRMED_CONTINUITY_%d" % index,
                "summary": "确认摘要%d" % index,
                "locked": index == 2,
            })
            beats.append({
                "index": index,
                "phase": "阶段%d" % index,
                "summary": "确认摘要%d" % index,
                "duration": 5,
            })
        contract = {
            "schema_version": "preproject-confirmed-shot-contract-v1",
            "title": "确认短剧",
            "logline": "长" * 1000,
            "protagonist": "林夏",
            "conflict": "是否相信旧友",
            "ending": "两人完成和解",
            "ratio": "16:9",
            "duration_seconds": 30,
            "shot_count": 6,
            "genre": "悬疑推理",
            "visual_style": "电影感写实",
            "creative_memory": {
                "schema_version": "short-drama-creative-memory-v1",
                "fields": {
                    "topic": "旧友重逢", "protagonist": "林夏",
                    "conflict": "是否相信旧友", "emotion": "温暖克制",
                    "ending": "两人完成和解", "audience": "年轻人",
                    "style": "电影感写实",
                },
            },
            "story_plan": {
                "schema_version": "short-drama-story-plan-v1",
                "premise": "旧友在雨夜重逢", "theme": "信任与和解",
                "audience": "年轻人", "emotion": "温暖克制",
                "dramatic_question": "林夏能否在末班车前说出真相？",
                "character_goal": "在末班车前说出真相", "obstacle": "是否相信旧友",
                "stakes": "失败会永远失去这段关系", "hook": "旧友突然出现",
                "turning_point": "旧信证明当年的误会", "climax": "林夏选择相信旧友",
                "resolution": "两人完成和解",
                "acts": [
                    {"act": 1, "name": "建立", "purpose": "建立处境", "summary": "旧友出现"},
                    {"act": 2, "name": "冲突", "purpose": "升级阻力", "summary": "旧信出现"},
                    {"act": 3, "name": "选择", "purpose": "兑现结局", "summary": "完成和解"},
                ],
            },
            "scenes": [
                {"index": 1, "phase": "建立", "location": "雨夜车站", "characters": ["林夏", "周野"], "objective": "建立误会", "conflict": "林夏拒绝交流", "turn": "周野拿出旧信", "shot_start": 1, "shot_end": 3},
                {"index": 2, "phase": "选择", "location": "站台", "characters": ["林夏", "周野"], "objective": "完成选择", "conflict": "末班车即将离开", "turn": "林夏留下", "shot_start": 4, "shot_end": 6},
            ],
            "script_review": {"schema_version": "short-drama-script-review-v1", "score": 94, "status": "needs_revision", "issues": [{"severity": "warning", "scope": "shot", "index": 1, "code": "performance_tight", "message": "表演时间略紧", "repairable": True}]},
            "characters": ["林夏", "周野"],
            "beats": beats,
            "shots": shots,
        }
        for length in (600, 1000):
            boundary = dict(contract, logline="边" * length)
            normalized = short_drama_conversation._normalize_confirmed_contract(
                self.project, boundary
            )
            self.assertEqual(length, len(normalized["logline"]))
        expanded_shot_contract = json.loads(json.dumps(contract))
        expanded_shot_contract["shots"][0]["camera"] = "运" * 300
        expanded_shot_contract["shots"][0]["continuity"] = "续" * 360
        expanded = short_drama_conversation._normalize_confirmed_contract(
            self.project, expanded_shot_contract
        )
        self.assertEqual(300, len(expanded["shots"][0]["camera"]))
        self.assertEqual(360, len(expanded["shots"][0]["continuity"]))
        generated = short_drama_conversation.generate_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
                "instruction": "持久化用户已确认的逐镜合同",
                "confirmed_contract": contract,
            },
            "confirmed-contract-generate",
        )
        self.assertEqual("script_review", generated["conversation"]["state"])
        script = generated["current_script"]["script"]
        self.assertEqual(contract, script["confirmed_contract"])
        self.assertEqual("悬疑推理", script["overview"]["genre"])
        self.assertIn("题材：悬疑推理", script["shots"][0]["provider_prompt"])
        self.assertEqual("CONFIRMED_SOUND_1", script["shots"][0]["sound"])
        self.assertEqual("CONFIRMED_TRANSITION_1", script["shots"][0]["transition"])
        self.assertEqual("CONFIRMED_CONTINUITY_1", script["shots"][0]["continuity"])
        self.assertEqual("CONFIRMED_ACTION_1", script["shots"][0]["action"])
        self.assertEqual("CONFIRMED_EXPRESSION_1", script["shots"][0]["expression"])
        self.assertEqual("这是确认台词", script["dialogue_lines"][0]["text"])

        locked = short_drama_conversation.lock_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": generated["conversation"]["revision"],
                "version_id": generated["current_script"]["id"],
            },
            "confirmed-contract-lock",
        )
        self.assertEqual("script_locked", locked["conversation"]["state"])
        self.assertEqual(contract, locked["current_script"]["script"]["confirmed_contract"])

    def test_legacy_confirmed_contract_without_genre_remains_supported(self):
        project = short_drama.create_project(
            self.db, "alice", payload(genre="")
        )
        contract = confirmed_contract()
        contract.pop("genre", None)

        normalized = short_drama_conversation._normalize_confirmed_contract(
            project, contract
        )
        self.assertEqual("", normalized["genre"])

        script = short_drama_conversation._script_from_confirmed_contract(
            project, contract, ""
        )
        self.assertEqual("", script["overview"]["genre"])
        self.assertNotIn("题材：", script["shots"][0]["provider_prompt"])

    def import_payload(self, source, **changes):
        value = {
            "title": "完整导入剧本",
            "synopsis": "完整原稿导入后按原有人物、剧情和对白生成。",
            "ratio": "16:9",
            "target_duration": 30,
            "shot_count": 6,
            "visual_style": "电影感写实",
            "source_text": source,
            "filename": "完整剧本.txt",
            "import_mode": "faithful",
        }
        value.update(changes)
        return value

    def test_import_filename_limit_rejects_instead_of_truncating(self):
        source = "A complete live action script."
        accepted = short_drama.import_script_project(
            self.db, "alice", self.import_payload(source, filename="f" * 255),
            "filename-255",
        )
        conn = self.db()
        try:
            stored_filename = conn.execute(
                "SELECT filename FROM short_drama_script_imports WHERE project_id=?",
                (accepted["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("f" * 255, stored_filename)
        with self.assertRaises(short_drama.ScriptImportError) as rejected:
            short_drama.import_script_project(
                self.db, "alice", self.import_payload(source, filename="f" * 256),
                "filename-256",
            )
        self.assertEqual("invalid_filename", rejected.exception.code)
        self.assertEqual(400, rejected.exception.status)

    def test_live_action_derived_identity_limit_is_explicit_and_atomic(self):
        contract = [{
            "character_key": "character_1", "name": "Boundary Role",
            "role_type": "main", "gender": "g" * 500, "age": "a" * 500,
            "identity_text": "i" * 500, "relationships": "r" * 492,
            "personality": "", "face_shape": "", "hairstyle": "",
            "hair_color": "", "height_body": "", "fixed_clothing": "coat",
            "fixed_colors": "", "accessories": "", "appearance_prompt": "",
            "wardrobe_prompt": "", "reference_views": [
                "front_full", "side_full", "back_full",
            ],
        }]
        project = short_drama.import_script_project(
            self.db, "alice", self.import_payload(
                "A complete boundary script.", content_type="live_action",
                character_contract=contract,
            ), "identity-2000",
        )
        self.assertEqual(2000, len(project["characters"][0]["identity_text"]))
        conn = self.db()
        try:
            before = {
                "revision": conn.execute(
                    "SELECT revision FROM short_drama_projects WHERE id=?", (project["id"],),
                ).fetchone()[0],
                "characters": conn.execute(
                    "SELECT character_key,identity_text FROM short_drama_characters "
                    "WHERE project_id=?", (project["id"],),
                ).fetchall(),
                "contract": conn.execute(
                    "SELECT character_contract_json FROM short_drama_script_imports "
                    "WHERE project_id=?", (project["id"],),
                ).fetchone()[0],
            }
        finally:
            conn.close()
        too_long_contract = [dict(contract[0], relationships="r" * 493)]
        too_long_characters = [dict(
            project["characters"][0], identity_text="x" * 2001,
        )]
        with self.assertRaises(short_drama.ScriptImportError) as update_error:
            short_drama.update_characters(
                self.db, "alice", project["id"], project["revision"],
                too_long_characters, character_contract=too_long_contract,
            )
        self.assertEqual("character_identity_too_long", update_error.exception.code)
        self.assertEqual(400, update_error.exception.status)
        conn = self.db()
        try:
            self.assertEqual(before["revision"], conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?", (project["id"],),
            ).fetchone()[0])
            self.assertEqual(before["characters"], conn.execute(
                "SELECT character_key,identity_text FROM short_drama_characters "
                "WHERE project_id=?", (project["id"],),
            ).fetchall())
            self.assertEqual(before["contract"], conn.execute(
                "SELECT character_contract_json FROM short_drama_script_imports "
                "WHERE project_id=?", (project["id"],),
            ).fetchone()[0])
        finally:
            conn.close()
        with self.assertRaises(short_drama.ScriptImportError) as import_error:
            short_drama.import_script_project(
                self.db, "alice", self.import_payload(
                    "A second complete boundary script.", content_type="live_action",
                    character_contract=too_long_contract,
                ), "identity-2001",
            )
        self.assertEqual("character_identity_too_long", import_error.exception.code)
        self.assertEqual(400, import_error.exception.status)
        conn = self.db()
        try:
            self.assertIsNone(conn.execute(
                "SELECT project_id FROM short_drama_script_imports "
                "WHERE idempotency_key='identity-2001'",
            ).fetchone())
        finally:
            conn.close()

    def test_replacing_saved_live_action_draft_uses_second_story_snapshot(self):
        contract = [{
            "character_key": "character_1", "name": "Lin Xia",
            "role_type": "main", "gender": "female", "identity_text": "clerk",
            "relationships": "", "personality": "calm", "age": "26",
            "face_shape": "oval", "hairstyle": "short", "hair_color": "black",
            "height_body": "165cm", "fixed_clothing": "white shirt",
            "fixed_colors": "white", "accessories": "watch",
            "appearance_prompt": "cinematic portrait", "wardrobe_prompt": "white shirt",
            "reference_views": ["front_full", "side_full", "back_full"],
        }]
        scenarios = (
            ("title", {"title": "Second confirmed title"}),
            ("source", {"source_text": "Second confirmed source with a new ending."}),
            ("spec", {
                "target_duration": 45, "shot_count": 9,
                "visual_style": "documentary realism",
            }),
        )
        for index, (label, changes) in enumerate(scenarios):
            with self.subTest(change=label):
                first_body = self.import_payload(
                    "First complete source before editing.", title="First title",
                    target_duration=30, shot_count=6, visual_style="cinematic",
                    content_type="live_action", character_contract=contract,
                )
                first = short_drama.import_script_project(
                    self.db, "alice", first_body, "replace-old-%d" % index,
                )
                saved = short_drama.update_characters(
                    self.db, "alice", first["id"], first["revision"],
                    short_drama._characters_from_import_contract(contract),
                    character_contract=contract,
                )
                with mock.patch.object(
                        short_drama, "_has_unapplied_charged_job", return_value=False):
                    self.assertTrue(short_drama.delete_project(
                        self.db, "alice", saved["id"], saved["revision"],
                    ))
                second_body = dict(first_body)
                second_body.update(changes)
                second = short_drama.import_script_project(
                    self.db, "alice", second_body, "replace-new-%d" % index,
                )
                conn = self.db()
                try:
                    project_row = conn.execute(
                        "SELECT title,target_duration,shot_count,visual_style "
                        "FROM short_drama_projects WHERE id=? AND deleted=0",
                        (second["id"],),
                    ).fetchone()
                    import_row = conn.execute(
                        "SELECT source_text,character_contract_json "
                        "FROM short_drama_script_imports WHERE project_id=?",
                        (second["id"],),
                    ).fetchone()
                finally:
                    conn.close()
                self.assertEqual(second_body["title"], project_row[0])
                self.assertEqual(second_body["target_duration"], project_row[1])
                self.assertEqual(second_body["shot_count"], project_row[2])
                self.assertEqual(second_body["visual_style"], project_row[3])
                self.assertEqual(second_body["source_text"], import_row[0])
                self.assertEqual(contract, json.loads(import_row[1]))
                with mock.patch.object(
                        short_drama, "_has_unapplied_charged_job", return_value=False):
                    self.assertTrue(short_drama.delete_project(
                        self.db, "alice", second["id"], second["revision"],
                    ))

    def test_live_action_import_persists_confirmed_character_contract(self):
        contract = [{
            "character_key": "character_1",
            "name": "林夏",
            "role_type": "main",
            "gender": "女",
            "identity_text": "便利店店员",
            "relationships": "与周野是同事",
            "personality": "克制、善良",
            "age": "26 岁",
            "face_shape": "鹅蛋脸",
            "hairstyle": "黑色齐肩短发",
            "hair_color": "黑色",
            "height_body": "165cm，匀称",
            "fixed_clothing": "米白衬衫和深色长裤",
            "fixed_colors": "米白、深灰",
            "accessories": "银色腕表",
            "appearance_prompt": "年轻女性，鹅蛋脸，黑色齐肩短发",
            "wardrobe_prompt": "固定穿米白衬衫和深色长裤",
            "reference_views": ["front_full", "side_full", "back_full"],
        }]
        source = "人物：林夏\n林夏：雨停以后我们就出发。"
        verify = lambda _: {"username": "alice"}
        import_request = Handler(
            "/api/gen/short-drama/projects/import",
            body=self.import_payload(
                source, content_type="live_action", character_contract=contract,
            ),
            idempotency_key="live-action-contract-1",
        )
        self.assertTrue(short_drama.dispatch_http(import_request, "POST", self.db, verify))
        self.assertEqual(200, import_request.response[0])
        imported = import_request.response[1]
        self.assertEqual("live_action", imported["script_import"]["content_type"])
        self.assertEqual(1, imported["script_import"]["role_count"])
        self.assertEqual(len(source), imported["script_import"]["character_count"])
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT content_type,character_contract_json "
                "FROM short_drama_script_imports WHERE project_id=?",
                (imported["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual("live_action", row[0])
        self.assertEqual(contract, json.loads(row[1]))
        workspace_request = Handler(
            "/api/gen/short-drama/conversation?project_id=" + imported["id"]
        )
        self.assertTrue(short_drama.dispatch_http(workspace_request, "GET", self.db, verify))
        self.assertEqual(200, workspace_request.response[0])
        workspace = workspace_request.response[1]
        self.assertEqual(
            contract,
            workspace["conversation"]["understanding"]["import_contract"]["character_contract"],
        )

        conn = self.db()
        try:
            before_conflict = {
                "revision": conn.execute(
                    "SELECT revision FROM short_drama_projects WHERE id=?",
                    (imported["id"],),
                ).fetchone()[0],
                "characters": conn.execute(
                    "SELECT character_key,name,identity_text,personality,source_type,avatar_id,"
                    "appearance_prompt,wardrobe_prompt,voice_key,voice_settings_json "
                    "FROM short_drama_characters "
                    "WHERE project_id=? ORDER BY sort_order,id",
                    (imported["id"],),
                ).fetchall(),
                "contract": conn.execute(
                    "SELECT character_contract_json FROM short_drama_script_imports "
                    "WHERE project_id=?",
                    (imported["id"],),
                ).fetchone()[0],
            }
        finally:
            conn.close()
        bypass_characters = short_drama._characters_from_import_contract(contract)
        bypass_characters[0]["identity_text"] = "tampered characters-only identity"
        bypass_request = Handler(
            "/api/gen/short-drama/project?id=" + imported["id"], body={
                "revision": imported["revision"],
                "characters": bypass_characters,
            },
        )
        self.assertTrue(short_drama.dispatch_http(bypass_request, "PUT", self.db, verify))
        self.assertEqual(400, bypass_request.response[0])
        conn = self.db()
        try:
            self.assertEqual(before_conflict["revision"], conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?",
                (imported["id"],),
            ).fetchone()[0])
            self.assertEqual(before_conflict["characters"], conn.execute(
                "SELECT character_key,name,identity_text,personality,source_type,avatar_id,"
                "appearance_prompt,wardrobe_prompt,voice_key,voice_settings_json "
                "FROM short_drama_characters WHERE project_id=? ORDER BY sort_order,id",
                (imported["id"],),
            ).fetchall())
            self.assertEqual(before_conflict["contract"], conn.execute(
                "SELECT character_contract_json FROM short_drama_script_imports "
                "WHERE project_id=?",
                (imported["id"],),
            ).fetchone()[0])
        finally:
            conn.close()
        conflicting_characters = short_drama._characters_from_import_contract(contract)
        conflicting_characters[0]["appearance_prompt"] = "conflicting appearance"
        conflict_request = Handler(
            "/api/gen/short-drama/project?id=" + imported["id"], body={
                "revision": imported["revision"],
                "characters": conflicting_characters,
                "character_contract": contract,
            },
        )
        self.assertTrue(short_drama.dispatch_http(conflict_request, "PUT", self.db, verify))
        self.assertEqual(400, conflict_request.response[0])
        conn = self.db()
        try:
            self.assertEqual(before_conflict["revision"], conn.execute(
                "SELECT revision FROM short_drama_projects WHERE id=?",
                (imported["id"],),
            ).fetchone()[0])
            self.assertEqual(before_conflict["characters"], conn.execute(
                "SELECT character_key,name,identity_text,personality,source_type,avatar_id,"
                "appearance_prompt,wardrobe_prompt,voice_key,voice_settings_json "
                "FROM short_drama_characters "
                "WHERE project_id=? ORDER BY sort_order,id",
                (imported["id"],),
            ).fetchall())
            self.assertEqual(before_conflict["contract"], conn.execute(
                "SELECT character_contract_json FROM short_drama_script_imports "
                "WHERE project_id=?",
                (imported["id"],),
            ).fetchone()[0])
        finally:
            conn.close()

        changed_contract = [dict(contract[0], name="Lin Xia Updated"), {
            **dict(contract[0]),
            "character_key": "character_2",
            "name": "Zhou Ye",
            "role_type": "support",
        }]
        update_request = Handler(
            "/api/gen/short-drama/project?id=" + imported["id"], body={
                "revision": imported["revision"],
                "characters": short_drama._characters_from_import_contract(changed_contract),
                "character_contract": changed_contract,
            },
        )
        self.assertTrue(short_drama.dispatch_http(update_request, "PUT", self.db, verify))
        self.assertEqual(200, update_request.response[0])
        updated = update_request.response[1]
        changed_workspace_request = Handler(
            "/api/gen/short-drama/conversation?project_id=" + imported["id"]
        )
        self.assertTrue(short_drama.dispatch_http(changed_workspace_request, "GET", self.db, verify))
        self.assertEqual(200, changed_workspace_request.response[0])
        changed_workspace = changed_workspace_request.response[1]
        changed_import_contract = changed_workspace["conversation"]["understanding"]["import_contract"]
        self.assertEqual(["Lin Xia Updated", "Zhou Ye"], changed_import_contract["characters"])
        self.assertEqual(changed_contract, changed_import_contract["character_contract"])

        final_contract = [changed_contract[1]]
        final_update_request = Handler(
            "/api/gen/short-drama/project?id=" + imported["id"], body={
                "revision": updated["revision"],
                "characters": short_drama._characters_from_import_contract(final_contract),
                "character_contract": final_contract,
            },
        )
        self.assertTrue(short_drama.dispatch_http(final_update_request, "PUT", self.db, verify))
        self.assertEqual(200, final_update_request.response[0])
        final_workspace_request = Handler(
            "/api/gen/short-drama/conversation?project_id=" + imported["id"]
        )
        self.assertTrue(short_drama.dispatch_http(final_workspace_request, "GET", self.db, verify))
        self.assertEqual(200, final_workspace_request.response[0])
        final_workspace = final_workspace_request.response[1]
        final_import_contract = final_workspace["conversation"]["understanding"]["import_contract"]
        self.assertEqual(["Zhou Ye"], final_import_contract["characters"])
        self.assertEqual(final_contract, final_import_contract["character_contract"])

    def test_full_import_is_atomic_idempotent_and_generation_uses_all_anchors(self):
        start = "MARKER_START_72 开场关键对白。\n"
        middle = "MARKER_MIDDLE_72 中段关键转折。\n"
        end = "MARKER_END_72 结尾关键对白。"
        filler_size = 50000 - len(start) - len(middle) - len(end)
        left = filler_size // 2
        source = start + ("甲" * left) + middle + ("乙" * (filler_size - left)) + end
        body = self.import_payload(source)
        imported = short_drama.import_script_project(
            self.db, "alice", body, "full-import-72",
        )
        replay = short_drama.import_script_project(
            self.db, "alice", body, "full-import-72",
        )
        self.assertEqual(imported["id"], replay["id"])
        self.assertTrue(replay["script_import"]["replayed"])
        with self.assertRaises(short_drama.ScriptImportError) as conflict:
            short_drama.import_script_project(
                self.db, "alice",
                self.import_payload(source[:-1] + "改"),
                "full-import-72",
            )
        self.assertEqual("idempotency_conflict", conflict.exception.code)
        workspace = short_drama_conversation.workspace(
            self.db, "alice", "alice", imported["id"],
        )
        self.assertEqual(50000, workspace["script_import"]["character_count"])
        with self.assertRaises(short_drama.ScriptImportError) as too_long:
            short_drama.import_script_project(
                self.db, "alice", self.import_payload(source + "X"), "source-50001",
            )
        self.assertEqual("script_too_long", too_long.exception.code)
        self.assertEqual(413, too_long.exception.status)
        self.assertEqual("import_review", workspace["conversation"]["understanding"]["phase"])
        self.assertEqual("import_understanding", workspace["messages"][0]["metadata"]["kind"])
        with self.assertRaises(short_drama_conversation.ConversationError) as blocked:
            short_drama_conversation.generate_script(
                self.db, "alice", "alice", {
                    "project_id": imported["id"],
                    "conversation_revision": 1,
                    "instruction": "尊重原稿",
                }, "generate-unconfirmed-import-72",
            )
        self.assertEqual("direction_confirmation_required", blocked.exception.code)
        confirmed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": imported["id"],
                "conversation_revision": 1,
                "message": "确认尊重原稿并生成",
            }, "confirm-full-import-72",
        )
        self.assertTrue(confirmed["conversation"]["understanding"]["direction_confirmed"])
        compiler = short_drama_conversation.short_drama_storyboard.compile_storyboard
        captured = {}

        def capture_compiler(project, clauses, *args, **kwargs):
            captured["clauses"] = clauses
            return compiler(project, clauses, *args, **kwargs)

        with mock.patch.object(
            short_drama_conversation.short_drama_storyboard,
            "compile_storyboard",
            side_effect=capture_compiler,
        ):
            generated = short_drama_conversation.generate_script(
                self.db, "alice", "alice", {
                    "project_id": imported["id"],
                    "conversation_revision": confirmed["conversation"]["revision"],
                    "instruction": "尊重原稿",
                }, "generate-full-import-72",
            )
        compiler_input = "\n".join(captured["clauses"])
        self.assertGreaterEqual(len(compiler_input), 49990)
        self.assertIn("MARKER_START_72", compiler_input)
        self.assertIn("MARKER_MIDDLE_72", compiler_input)
        self.assertIn("MARKER_END_72", compiler_input)
        contract = generated["current_script"]["script"]["source_import"]
        anchors = "".join(item["excerpt"] for item in contract["anchors"])
        self.assertIn("MARKER_START_72", anchors)
        self.assertIn("MARKER_MIDDLE_72", anchors)
        self.assertIn("MARKER_END_72", anchors)
        self.assertEqual("faithful", contract["import_mode"])
        self.assertEqual(
            hashlib.sha256(source.encode("utf-8")).hexdigest(),
            contract["source_hash"],
        )
        conn = self.db()
        try:
            self.assertEqual(
                (1, 50000),
                conn.execute(
                    "SELECT COUNT(*),length(source_text) "
                    "FROM short_drama_script_imports WHERE project_id=?",
                    (imported["id"],),
                ).fetchone(),
            )
        finally:
            conn.close()

    def test_import_modes_have_distinct_confirmed_generation_contracts(self):
        source = (
            "场景一 雨夜车站\n"
            "林夏：别走。\n"
            "场景二 录音揭开误会\n"
            "周明：真相在这里。\n"
            "场景三 清晨重逢\n"
            "林夏：我会回来。"
        )
        projects = {}
        for mode in ("faithful", "optimize"):
            projects[mode] = short_drama.import_script_project(
                self.db, "alice", self.import_payload(source, import_mode=mode),
                "mode-contract-" + mode,
            )
            workspace = short_drama_conversation.workspace(
                self.db, "alice", "alice", projects[mode]["id"],
            )
            understanding = workspace["conversation"]["understanding"]
            self.assertEqual("import_review", understanding["phase"])
            self.assertFalse(understanding["direction_confirmed"])
            self.assertEqual(mode, understanding["import_contract"]["import_mode"])
            self.assertEqual(3, len(understanding["import_contract"]["key_dialogues"]))
            if mode == "faithful":
                self.assertEqual([], understanding["import_contract"]["proposed_changes"])
            else:
                self.assertTrue(all(
                    item["status"] == "pending"
                    for item in understanding["import_contract"]["proposed_changes"]
                ))
            with self.assertRaises(short_drama_conversation.ConversationError) as blocked:
                short_drama_conversation.generate_script(
                    self.db, "alice", "alice", {
                        "project_id": projects[mode]["id"],
                        "conversation_revision": 1,
                    }, "blocked-mode-" + mode,
                )
            self.assertEqual("direction_confirmation_required", blocked.exception.code)

        generated = {}
        generated_responses = {}
        for mode, confirmation in (
            ("faithful", "确认尊重原稿并生成"),
            ("optimize", "确认优化范围"),
        ):
            confirmed = short_drama_conversation.send_message(
                self.db, "alice", "alice", {
                    "project_id": projects[mode]["id"],
                    "conversation_revision": 1,
                    "message": confirmation,
                }, "confirm-mode-" + mode,
            )
            contract = confirmed["conversation"]["understanding"]["import_contract"]
            self.assertEqual(contract["source_hash"], contract["confirmed_source_hash"])
            self.assertEqual(mode, contract["confirmed_import_mode"])
            self.assertEqual(contract["revision"], contract["confirmed_contract_revision"])
            self.assertEqual(contract["contract_hash"], contract["confirmed_contract_hash"])
            confirmation_message = next(
                item for item in reversed(confirmed["messages"])
                if item["role"] == "user"
            )
            self.assertEqual(contract["revision"], confirmation_message["metadata"]["contract_revision"])
            self.assertEqual(contract["contract_hash"], confirmation_message["metadata"]["contract_hash"])
            generated_responses[mode] = short_drama_conversation.generate_script(
                self.db, "alice", "alice", {
                    "project_id": projects[mode]["id"],
                    "conversation_revision": confirmed["conversation"]["revision"],
                }, "generate-mode-" + mode,
            )
            generated[mode] = generated_responses[mode]["current_script"]["script"]

        faithful = generated["faithful"]
        optimize = generated["optimize"]
        self.assertEqual("faithful_preservation", faithful["import_behavior"])
        faithful_lines = [
            item["text"] for item in faithful["dialogue_lines"] if item["text"]
        ]
        for line in ("别走。", "真相在这里。", "我会回来。"):
            self.assertIn(line, faithful_lines)
        self.assertLess(faithful_lines.index("别走。"), faithful_lines.index("真相在这里。"))
        self.assertLess(faithful_lines.index("真相在这里。"), faithful_lines.index("我会回来。"))
        self.assertEqual(
            3,
            len([item for item in faithful["preservation_map"] if item["kind"] == "dialogue"]),
        )
        self.assertEqual("confirmed_optimization", optimize["import_behavior"])
        self.assertEqual("confirmed", optimize["optimization_plan"]["status"])
        self.assertTrue(all(
            item["status"] == "confirmed"
            for item in optimize["optimization_plan"]["changes"]
        ))
        self.assertNotEqual(
            [item["text"] for item in faithful["dialogue_lines"]],
            [item["text"] for item in optimize["dialogue_lines"]],
        )
        changed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": projects["optimize"]["id"],
                "conversation_revision": generated_responses["optimize"]["conversation"]["revision"],
                "message": "修改优化范围：不要修改对白，只优化结构",
            }, "change-confirmed-optimization",
        )
        changed_understanding = changed["conversation"]["understanding"]
        self.assertTrue(changed_understanding["confirmation_invalidated"])
        changed_contract = changed_understanding["import_contract"]
        self.assertEqual(2, changed_contract["revision"])
        self.assertNotEqual(
            optimize["source_import"]["contract_hash"],
            changed_contract["contract_hash"],
        )
        enabled = {
            item["key"]: item["enabled"]
            for item in changed_contract["proposed_changes"]
        }
        self.assertEqual({
            "structure_pacing": True,
            "dialogue_polish": False,
            "visual_adaptation": False,
        }, enabled)
        with self.assertRaises(short_drama_conversation.ConversationError) as blocked:
            short_drama_conversation.generate_script(
                self.db, "alice", "alice", {
                    "project_id": projects["optimize"]["id"],
                    "conversation_revision": changed["conversation"]["revision"],
                }, "regenerate-unconfirmed-optimization",
            )
        self.assertEqual("direction_confirmation_required", blocked.exception.code)

        replayed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": projects["optimize"]["id"],
                "conversation_revision": generated_responses["optimize"]["conversation"]["revision"],
                "message": "修改优化范围：不要修改对白，只优化结构",
            }, "change-confirmed-optimization",
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(
            2,
            replayed["conversation"]["understanding"]["import_contract"]["revision"],
        )
        reconfirmed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": projects["optimize"]["id"],
                "conversation_revision": changed["conversation"]["revision"],
                "message": "确认调整后的原稿理解",
            }, "reconfirm-optimization-contract",
        )
        regenerated = short_drama_conversation.generate_script(
            self.db, "alice", "alice", {
                "project_id": projects["optimize"]["id"],
                "conversation_revision": reconfirmed["conversation"]["revision"],
            }, "regenerate-confirmed-optimization",
        )
        regenerated_script = regenerated["current_script"]["script"]
        self.assertEqual(
            ["structure_pacing"],
            [
                item["key"]
                for item in regenerated_script["optimization_plan"]["changes"]
            ],
        )
        self.assertEqual(
            {"dialogue_polish", "visual_adaptation"},
            {
                item["key"] for item in
                regenerated_script["optimization_plan"]["excluded_changes"]
            },
        )
        self.assertEqual(2, regenerated_script["source_import"]["contract_revision"])

    def test_composite_import_confirmation_requires_reconfirmation(self):
        source = "场景一 雨夜车站\n林夏：别走。\n周明：真相在这里。"
        imported = short_drama.import_script_project(
            self.db, "alice",
            self.import_payload(source, import_mode="optimize"),
            "composite-import-confirmation",
        )
        changed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": imported["id"],
                "conversation_revision": 1,
                "message": "确认，但不要修改对白，只优化结构",
            }, "composite-import-change",
        )
        understanding = changed["conversation"]["understanding"]
        self.assertFalse(understanding["direction_confirmed"])
        contract = understanding["import_contract"]
        self.assertEqual(2, contract["revision"])
        self.assertEqual({
            "structure_pacing": True,
            "dialogue_polish": False,
            "visual_adaptation": False,
        }, {
            item["key"]: item["enabled"]
            for item in contract["proposed_changes"]
        })
        user_message = next(
            item for item in reversed(changed["messages"])
            if item["role"] == "user"
        )
        self.assertEqual("import_contract_change", user_message["metadata"]["kind"])
        with self.assertRaises(short_drama_conversation.ConversationError) as blocked:
            short_drama_conversation.generate_script(
                self.db, "alice", "alice", {
                    "project_id": imported["id"],
                    "conversation_revision": changed["conversation"]["revision"],
                }, "composite-import-generate-before-reconfirm",
            )
        self.assertEqual("direction_confirmation_required", blocked.exception.code)

        reconfirmed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": imported["id"],
                "conversation_revision": changed["conversation"]["revision"],
                "message": "确认调整后的优化范围",
            }, "composite-import-reconfirm",
        )
        self.assertTrue(
            reconfirmed["conversation"]["understanding"]["direction_confirmed"]
        )
        generated = short_drama_conversation.generate_script(
            self.db, "alice", "alice", {
                "project_id": imported["id"],
                "conversation_revision": reconfirmed["conversation"]["revision"],
            }, "composite-import-generate",
        )
        self.assertEqual(
            ["structure_pacing"],
            [
                item["key"] for item in
                generated["current_script"]["script"]["optimization_plan"]["changes"]
            ],
        )

    def test_import_optimization_question_is_not_confirmation(self):
        imported = short_drama.import_script_project(
            self.db, "alice",
            self.import_payload(
                "场景一 雨夜车站\n林夏：别走。", import_mode="optimize",
            ),
            "question-import-confirmation",
        )
        changed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": imported["id"],
                "conversation_revision": 1,
                "message": "可以只优化结构，不要改对白吗？",
            }, "question-import-change",
        )
        understanding = changed["conversation"]["understanding"]
        self.assertFalse(understanding["direction_confirmed"])
        self.assertEqual(2, understanding["import_contract"]["revision"])
        self.assertEqual(
            "import_contract_change",
            next(
                item for item in reversed(changed["messages"])
                if item["role"] == "user"
            )["metadata"]["kind"],
        )

    def test_composite_faithful_confirmation_versions_preservation(self):
        source = "场景一 雨夜车站\n林夏：别走。\n周明：真相在这里。"
        imported = short_drama.import_script_project(
            self.db, "alice", self.import_payload(source),
            "composite-faithful-confirmation",
        )
        changed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": imported["id"],
                "conversation_revision": 1,
                "message": "确认，并新增必须保留对白：“真相在这里。”",
            }, "composite-faithful-change",
        )
        understanding = changed["conversation"]["understanding"]
        self.assertFalse(understanding["direction_confirmed"])
        contract = understanding["import_contract"]
        self.assertEqual(2, contract["revision"])
        self.assertEqual(1, len(contract["required_preservations"]))
        requirement = contract["required_preservations"][0]
        self.assertEqual("dialogue", requirement["kind"])
        self.assertEqual(source.index("真相在这里。"), requirement["source_offset"])
        self.assertEqual("真相在这里。", requirement["text"])
        self.assertEqual(
            "import_contract_change",
            next(
                item for item in reversed(changed["messages"])
                if item["role"] == "user"
            )["metadata"]["kind"],
        )

    def test_faithful_added_preservation_is_versioned_and_mapped(self):
        source = (
            "场景一 雨夜车站\n"
            "林夏：别走。\n"
            "场景二 录音揭开误会\n"
            "周明：真相在这里。\n"
            "场景三 清晨重逢\n"
            "林夏：我会回来。"
        )
        imported = short_drama.import_script_project(
            self.db, "alice", self.import_payload(source),
            "faithful-custom-preservation",
        )
        confirmed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": imported["id"], "conversation_revision": 1,
                "message": "确认尊重原稿并生成",
            }, "faithful-custom-confirm",
        )
        changed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": imported["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
                "message": "必须保留对白：“真相在这里。”",
            }, "faithful-custom-change",
        )
        understanding = changed["conversation"]["understanding"]
        self.assertTrue(understanding["confirmation_invalidated"])
        contract = understanding["import_contract"]
        self.assertEqual(2, contract["revision"])
        requirement = contract["required_preservations"][0]
        self.assertEqual("dialogue", requirement["kind"])
        self.assertEqual(source.index("真相在这里。"), requirement["source_offset"])
        self.assertEqual("真相在这里。", requirement["source"])
        reconfirmed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": imported["id"],
                "conversation_revision": changed["conversation"]["revision"],
                "message": "确认调整后的原稿理解",
            }, "faithful-custom-reconfirm",
        )
        generated = short_drama_conversation.generate_script(
            self.db, "alice", "alice", {
                "project_id": imported["id"],
                "conversation_revision": reconfirmed["conversation"]["revision"],
            }, "faithful-custom-generate",
        )
        script = generated["current_script"]["script"]
        mapping = next(
            item for item in script["preservation_map"]
            if item.get("requirement_id") == requirement["id"]
        )
        self.assertEqual(requirement["source_offset"], mapping["source_offset"])
        target_id = mapping["target"].split(".", 1)[1]
        target = next(item for item in script["dialogue_lines"] if item["id"] == target_id)
        self.assertIn("真相在这里。", target["text"])
        self.assertEqual(contract["contract_hash"], script["source_import"]["contract_hash"])

    def test_import_confirmation_is_invalidated_by_new_requirements(self):
        imported = short_drama.import_script_project(
            self.db, "alice",
            self.import_payload("林夏：保留这句。\n周明：结尾不要改。"),
            "invalidate-import-confirmation",
        )
        confirmed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": imported["id"], "conversation_revision": 1,
                "message": "确认尊重原稿并生成",
            }, "confirm-before-change",
        )
        changed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": imported["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
                "message": "修改要求：结尾需要保留原稿对白",
            }, "change-after-import-confirmation",
        )
        understanding = changed["conversation"]["understanding"]
        self.assertFalse(understanding["direction_confirmed"])
        self.assertTrue(understanding["confirmation_invalidated"])
        with self.assertRaises(short_drama_conversation.ConversationError) as blocked:
            short_drama_conversation.generate_script(
                self.db, "alice", "alice", {
                    "project_id": imported["id"],
                    "conversation_revision": changed["conversation"]["revision"],
                }, "generate-after-import-change",
            )
        self.assertEqual("direction_confirmation_required", blocked.exception.code)

    def test_import_snapshot_backfill_and_source_hash_change_require_confirmation(self):
        imported = short_drama.import_script_project(
            self.db, "alice", self.import_payload("林夏：保留原对白。"),
            "backfill-import-understanding",
        )
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_conversations SET understanding_json='{}' "
                "WHERE project_id=?", (imported["id"],),
            )
            conn.execute(
                "DELETE FROM short_drama_conversation_messages WHERE project_id=?",
                (imported["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        backfilled = short_drama_conversation.workspace(
            self.db, "alice", "alice", imported["id"],
        )
        self.assertEqual("import_review", backfilled["conversation"]["understanding"]["phase"])
        self.assertEqual("import_understanding", backfilled["messages"][0]["metadata"]["kind"])
        confirmed = short_drama_conversation.send_message(
            self.db, "alice", "alice", {
                "project_id": imported["id"], "conversation_revision": 1,
                "message": "确认尊重原稿并生成",
            }, "confirm-backfilled-import",
        )
        self.assertTrue(confirmed["conversation"]["understanding"]["direction_confirmed"])
        changed_source = "林夏：这是更新后的原对白。"
        changed_hash = hashlib.sha256(changed_source.encode("utf-8")).hexdigest()
        conn = self.db()
        try:
            conn.execute(
                "UPDATE short_drama_script_imports SET source_text=?,source_hash=? "
                "WHERE project_id=?",
                (changed_source, changed_hash, imported["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        refreshed = short_drama_conversation.workspace(
            self.db, "alice", "alice", imported["id"],
        )
        understanding = refreshed["conversation"]["understanding"]
        self.assertFalse(understanding["direction_confirmed"])
        self.assertEqual(changed_hash, understanding["import_contract"]["source_hash"])
        with self.assertRaises(short_drama_conversation.ConversationError) as blocked:
            short_drama_conversation.generate_script(
                self.db, "alice", "alice", {
                    "project_id": imported["id"],
                    "conversation_revision": refreshed["conversation"]["revision"],
                }, "generate-stale-import-confirmation",
            )
        self.assertEqual("direction_confirmation_required", blocked.exception.code)

    def test_import_failure_rolls_back_project_and_snapshot(self):
        conn = self.db()
        try:
            before = conn.execute(
                "SELECT COUNT(*) FROM short_drama_projects"
            ).fetchone()[0]
            conn.executescript("""
            CREATE TRIGGER reject_script_import
            BEFORE INSERT ON short_drama_script_imports
            BEGIN SELECT RAISE(ABORT,'injected import failure'); END;
            """)
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            short_drama.import_script_project(
                self.db, "alice", self.import_payload("场景一\n人物：完整对白。"),
                "rollback-import-72",
            )
        conn = self.db()
        try:
            self.assertEqual(
                before,
                conn.execute("SELECT COUNT(*) FROM short_drama_projects").fetchone()[0],
            )
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_script_imports "
                    "WHERE idempotency_key='rollback-import-72'"
                ).fetchone()[0],
            )
        finally:
            conn.close()

    def test_import_http_replays_one_project_after_lost_response(self):
        body = self.import_payload("场景一\n林夏：这是需要完整保存的关键对白。")
        first = Handler(
            "/api/gen/short-drama/projects/import", body=body,
            idempotency_key="http-import-72",
        )
        second = Handler(
            "/api/gen/short-drama/projects/import", body=body,
            idempotency_key="http-import-72",
        )
        verify = lambda _: {"username": "alice"}
        self.assertTrue(short_drama.dispatch_http(first, "POST", self.db, verify))
        self.assertTrue(short_drama.dispatch_http(second, "POST", self.db, verify))
        self.assertEqual(200, first.response[0])
        self.assertEqual(first.response[1]["id"], second.response[1]["id"])
        self.assertTrue(second.response[1]["script_import"]["replayed"])
        conn = self.db()
        try:
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_projects WHERE id=?",
                    (first.response[1]["id"],),
                ).fetchone()[0],
            )
        finally:
            conn.close()

    def test_project_create_http_replays_one_project_after_lost_response(self):
        body = payload(title="幂等创建短剧")
        first = Handler(
            "/api/gen/short-drama/projects", body=body,
            idempotency_key="http-create-lost-response",
        )
        second = Handler(
            "/api/gen/short-drama/projects", body=body,
            idempotency_key="http-create-lost-response",
        )
        verify = lambda _: {"username": "alice"}
        self.assertTrue(short_drama.dispatch_http(first, "POST", self.db, verify))
        self.assertTrue(short_drama.dispatch_http(second, "POST", self.db, verify))
        self.assertEqual(200, first.response[0])
        self.assertEqual(200, second.response[0])
        self.assertEqual(first.response[1]["id"], second.response[1]["id"])
        conn = self.db()
        try:
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_projects WHERE id=?",
                    (first.response[1]["id"],),
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_project_requests "
                    "WHERE username='alice' AND operation='project_create' "
                    "AND idempotency_key='http-create-lost-response'"
                ).fetchone()[0],
            )
        finally:
            conn.close()

    def test_planner_promotion_is_atomic_and_replays_after_lost_response(self):
        body = {
            "project": payload(title="原子确认项目"),
            "planning_messages": [
                "核心设定：雨夜重逢",
                "用户选择：情感治愈",
                "逐镜剧本：六个镜头均已人工确认",
            ],
            "confirmed_contract": confirmed_contract(),
        }

        class LostResponseHandler(Handler):
            def _send(self, _status, _payload):
                raise ConnectionAbortedError("response lost after commit")

        first = LostResponseHandler(
            "/api/gen/short-drama/projects/promote",
            body=body,
            idempotency_key="planner-promote-lost-response",
        )
        verify = lambda _: {"username": "alice"}
        with self.assertRaises(ConnectionAbortedError):
            short_drama.dispatch_http(first, "POST", self.db, verify)

        reloaded = Handler(
            "/api/gen/short-drama/projects/promote",
            body=body,
            idempotency_key="planner-promote-lost-response",
        )
        self.assertTrue(short_drama.dispatch_http(reloaded, "POST", self.db, verify))
        self.assertEqual(200, reloaded.response[0])
        result = reloaded.response[1]
        self.assertTrue(result["replayed"])
        self.assertEqual("script_locked", result["workspace"]["conversation"]["state"])
        self.assertEqual(
            body["confirmed_contract"],
            result["workspace"]["current_script"]["script"]["confirmed_contract"],
        )
        project_id = result["project"]["id"]
        conn = self.db()
        try:
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_projects "
                    "WHERE title='原子确认项目'"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_project_requests "
                    "WHERE username='alice' AND operation='planner_promote' "
                    "AND idempotency_key='planner-promote-lost-response'"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_script_snapshots "
                    "WHERE project_id=? AND status='locked'",
                    (project_id,),
                ).fetchone()[0],
            )
        finally:
            conn.close()

        conflict_body = dict(body)
        conflict_body["planning_messages"] = ["不同的策划内容"]
        conflict = Handler(
            "/api/gen/short-drama/projects/promote",
            body=conflict_body,
            idempotency_key="planner-promote-lost-response",
        )
        self.assertTrue(short_drama.dispatch_http(conflict, "POST", self.db, verify))
        self.assertEqual(409, conflict.response[0])
        self.assertEqual("idempotency_conflict", conflict.response[1]["code"])

    def test_planner_promotion_rolls_back_project_when_contract_write_fails(self):
        conn = self.db()
        try:
            conn.executescript("""
            CREATE TRIGGER reject_promoted_contract
            BEFORE INSERT ON short_drama_script_snapshots
            BEGIN SELECT RAISE(ABORT,'injected contract failure'); END;
            """)
            conn.commit()
        finally:
            conn.close()
        body = {
            "project": payload(title="必须回滚的确认项目"),
            "planning_messages": ["核心设定", "确认方向", "确认逐镜剧本"],
            "confirmed_contract": confirmed_contract(),
        }
        with self.assertRaises(sqlite3.IntegrityError):
            short_drama.promote_planner_project(
                self.db,
                "alice",
                body,
                "planner-promote-rollback",
            )
        conn = self.db()
        try:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_projects "
                    "WHERE title='必须回滚的确认项目'"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_project_requests "
                    "WHERE operation='planner_promote' "
                    "AND idempotency_key='planner-promote-rollback'"
                ).fetchone()[0],
            )
        finally:
            conn.close()

    def test_project_create_same_key_with_different_payload_conflicts(self):
        first = Handler(
            "/api/gen/short-drama/projects", body=payload(title="第一版项目"),
            idempotency_key="http-create-conflict",
        )
        conflict = Handler(
            "/api/gen/short-drama/projects", body=payload(title="第二版项目"),
            idempotency_key="http-create-conflict",
        )
        verify = lambda _: {"username": "alice"}
        self.assertTrue(short_drama.dispatch_http(first, "POST", self.db, verify))
        self.assertTrue(short_drama.dispatch_http(conflict, "POST", self.db, verify))
        self.assertEqual(200, first.response[0])
        self.assertEqual(409, conflict.response[0])
        self.assertEqual("idempotency_conflict", conflict.response[1]["code"])
        conn = self.db()
        try:
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_projects "
                    "WHERE title IN ('第一版项目','第二版项目')"
                ).fetchone()[0],
            )
        finally:
            conn.close()

    def test_project_create_rolls_back_when_idempotency_record_fails(self):
        conn = self.db()
        try:
            before = conn.execute("SELECT COUNT(*) FROM short_drama_projects").fetchone()[0]
            conn.executescript("""
            CREATE TRIGGER reject_project_request
            BEFORE INSERT ON short_drama_project_requests
            BEGIN SELECT RAISE(ABORT,'injected request failure'); END;
            """)
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            short_drama.create_project(
                self.db, "alice", payload(title="事务回滚项目"),
                idempotency_key="rollback-create-request",
            )
        conn = self.db()
        try:
            self.assertEqual(
                before,
                conn.execute("SELECT COUNT(*) FROM short_drama_projects").fetchone()[0],
            )
        finally:
            conn.close()

    def test_message_generate_restore_and_lock_flow(self):
        first = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": 1,
                "message": "结尾需要温暖反转，不要悲剧。",
            },
            "message-1",
        )
        self.assertEqual("direction_review", first["conversation"]["state"])
        self.assertEqual(2, len(first["messages"]))
        confirmed = self.confirm_direction(
            self.project["id"], first["conversation"]["revision"], "flow"
        )

        generated = short_drama_conversation.generate_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
                "instruction": "强化雨夜氛围",
            },
            "generate-1",
        )
        self.assertEqual("script_review", generated["conversation"]["state"])
        job = short_drama_conversation.get_job(
            self.db, "alice", self.project["id"], generated["job"]["id"]
        )
        self.assertEqual("succeeded", job["status"])
        self.assertEqual(generated["current_script"]["id"], job["result_version_id"])
        self.assertEqual(6, len(generated["current_script"]["script"]["shots"]))
        self.assertEqual(
            30,
            sum(
                item["duration_seconds"]
                for item in generated["current_script"]["script"]["shots"]
            ),
        )

        changed = short_drama_conversation.generate_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": generated["conversation"]["revision"],
                "instruction": "增加悬念",
            },
            "generate-2",
        )
        self.assertEqual(2, changed["current_script"]["version"])

        restored = short_drama_conversation.restore_version(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": changed["conversation"]["revision"],
                "version_id": generated["current_script"]["id"],
            },
            "restore-1",
        )
        self.assertEqual(3, restored["current_script"]["version"])
        self.assertIn("v1", restored["current_script"]["change_summary"])

        locked = short_drama_conversation.lock_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": restored["conversation"]["revision"],
                "version_id": restored["current_script"]["id"],
            },
            "lock-1",
        )
        self.assertEqual("script_locked", locked["conversation"]["state"])
        self.assertEqual("locked", locked["current_script"]["status"])
        conn = self.db()
        try:
            version_count = conn.execute(
                "SELECT COUNT(*) FROM short_drama_script_snapshots WHERE project_id=?",
                (self.project["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        with self.assertRaises(short_drama_conversation.ConversationError) as raised:
            short_drama_conversation.change_shot_structure(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "conversation_revision": locked["conversation"]["revision"],
                    "version_id": locked["current_script"]["id"],
                    "shot_key": locked["current_script"]["script"]["shots"][0]["shot_key"],
                    "action": "copy",
                },
                "structure-after-lock",
            )
        self.assertEqual("script_locked", raised.exception.code)
        self.assertEqual(409, raised.exception.status)
        after_rejection = short_drama_conversation.workspace(
            self.db, "alice", "alice", self.project["id"]
        )
        self.assertEqual(
            locked["conversation"]["revision"],
            after_rejection["conversation"]["revision"],
        )
        self.assertEqual(
            locked["current_script"]["id"], after_rejection["current_script"]["id"],
        )
        self.assertEqual(
            locked["conversation"]["locked_version_id"],
            after_rejection["conversation"]["locked_version_id"],
        )
        conn = self.db()
        try:
            self.assertEqual(
                version_count,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_script_snapshots WHERE project_id=?",
                    (self.project["id"],),
                ).fetchone()[0],
            )
        finally:
            conn.close()
        with self.assertRaises(short_drama_conversation.ConversationError):
            short_drama_conversation.generate_script(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "conversation_revision": locked["conversation"]["revision"],
                },
                "generate-after-lock",
            )

    def test_dialogue_shot_can_be_copied_through_public_structure_boundary(self):
        confirmed = self.confirm_direction(self.project["id"], 1, "copy-dialogue")
        generated = short_drama_conversation.generate_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
            },
            "copy-dialogue-generate",
        )
        original = generated["current_script"]
        original_lines = {
            item["id"]: item for item in original["script"]["dialogue_lines"]
        }
        source_index, source_shot = next(
            (index, shot)
            for index, shot in enumerate(original["script"]["shots"])
            if any(
                original_lines[line_id].get("text")
                for line_id in shot.get("dialogue_line_ids") or []
            )
        )
        source_line = original_lines[source_shot["dialogue_line_ids"][0]]

        copied = short_drama_conversation.change_shot_structure(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": generated["conversation"]["revision"],
                "version_id": original["id"],
                "shot_key": source_shot["shot_key"],
                "action": "copy",
            },
            "copy-dialogue-structure",
        )

        current = copied["current_script"]
        self.assertNotEqual(original["id"], current["id"])
        self.assertEqual(original["version"] + 1, current["version"])
        self.assertEqual(
            generated["conversation"]["revision"] + 1,
            copied["conversation"]["revision"],
        )
        self.assertEqual(
            len(original["script"]["shots"]) + 1,
            len(current["script"]["shots"]),
        )
        copied_shot = current["script"]["shots"][source_index + 1]
        self.assertNotEqual(source_shot["shot_key"], copied_shot["shot_key"])
        copied_lines = {
            item["id"]: item for item in current["script"]["dialogue_lines"]
        }
        copied_line = copied_lines[copied_shot["dialogue_line_ids"][0]]
        self.assertNotEqual(source_line["id"], copied_line["id"])
        self.assertEqual(source_line["text"], copied_line["text"])
        self.assertNotEqual("blocked", current["script"]["quality_gate"]["status"])

        conn = self.db()
        try:
            stored_original = json.loads(conn.execute(
                "SELECT script_json FROM short_drama_script_snapshots WHERE id=?",
                (original["id"],),
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(original["script"], stored_original)

        restored = short_drama_conversation.restore_version(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": copied["conversation"]["revision"],
                "version_id": original["id"],
            },
            "copy-dialogue-restore",
        )
        self.assertEqual(original["script"], restored["current_script"]["script"])

    def test_user_created_shots_can_be_regenerated_without_changing_identity(self):
        confirmed = self.confirm_direction(self.project["id"], 1, "user-shot-regen")
        current = short_drama_conversation.generate_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
            },
            "user-shot-regen-generate",
        )

        for index, action in enumerate(("copy", "insert_after", "smart_insert"), 1):
            before = current["current_script"]
            before_keys = {item["shot_key"] for item in before["script"]["shots"]}
            target_key = before["script"]["shots"][0]["shot_key"]
            structured = short_drama_conversation.change_shot_structure(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "conversation_revision": current["conversation"]["revision"],
                    "version_id": before["id"],
                    "shot_key": target_key,
                    "action": action,
                    "instruction": "bridge action %d" % index,
                },
                "user-shot-structure-%d" % index,
            )
            structured_script = structured["current_script"]["script"]
            user_shot = next(
                item for item in structured_script["shots"]
                if item["shot_key"] not in before_keys
            )
            shot_index = structured_script["shots"].index(user_shot)
            previous_visual = structured_script["shots"][shot_index - 1]["visual"]
            next_visual = structured_script["shots"][shot_index + 1]["visual"]
            line_id = user_shot["dialogue_line_ids"][0]
            duration = user_shot["duration_seconds"]

            with mock.patch.object(
                short_drama_conversation,
                "_script",
                side_effect=AssertionError("user-shot regeneration rebuilt the full script"),
            ):
                regenerated = short_drama_conversation.regenerate_shot(
                    self.db,
                    "alice",
                    "alice",
                    {
                        "project_id": self.project["id"],
                        "conversation_revision": structured["conversation"]["revision"],
                        "version_id": structured["current_script"]["id"],
                        "shot_key": user_shot["shot_key"],
                        "instruction": "regenerate user shot %d" % index,
                    },
                    "user-shot-regenerate-%d" % index,
                )
            regenerated_shot = regenerated["current_script"]["script"]["shots"][shot_index]
            self.assertEqual(user_shot["shot_key"], regenerated_shot["shot_key"])
            self.assertEqual([line_id], regenerated_shot["dialogue_line_ids"])
            self.assertEqual(duration, regenerated_shot["duration_seconds"])
            self.assertIn(previous_visual[:40], regenerated_shot["visual"])
            self.assertIn(next_visual[:40], regenerated_shot["visual"])
            self.assertNotEqual(
                structured["current_script"]["id"],
                regenerated["current_script"]["id"],
            )
            current = regenerated

    def test_regenerate_and_restore_invalidate_locked_scene_reference_atomically(self):
        confirmed = self.confirm_direction(self.project["id"], 1, "scene-invalidation")
        generated = short_drama_conversation.generate_script(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
            }, "scene-invalidation-generate",
        )
        original = generated["current_script"]
        shot_key = original["script"]["shots"][0]["shot_key"]
        edited = short_drama_conversation.update_shot(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "conversation_revision": generated["conversation"]["revision"],
                "version_id": original["id"], "shot_key": shot_key,
                "changes": {"scene": "old rainy street"},
            }, "scene-invalidation-edit",
        )
        short_drama_asset_graph.sync_foundation(
            self.db, "alice", "alice", self.project["id"],
        )
        scenes = short_drama_asset_graph.scene_workspace(
            self.db, "alice", self.project["id"],
        )
        old_scene = next(
            scene for scene in scenes["scenes"]
            if any(shot["shot_key"] == shot_key for shot in scene["shots"])
        )
        self.set_and_lock_scene_reference(scenes, old_scene["scene_key"], "old reference")
        self.assertIsNotNone(self.locked_scene_reference(shot_key))

        shot_id = next(
            shot["id"] for scene in scenes["scenes"] for shot in scene["shots"]
            if shot["shot_key"] == shot_key
        )
        with closing(self.db()) as conn:
            conn.execute(
                "INSERT INTO short_drama_assets "
                "(id,project_id,shot_id,type,current_version,locked,created_at,updated_at) "
                "VALUES ('stale-still',?,?, 'still',1,1,1,1)",
                (self.project["id"], shot_id),
            )
            conn.execute(
                "INSERT INTO short_drama_asset_versions "
                "(id,asset_id,version,job_id,url,prompt,ratio,status,created_at) "
                "VALUES ('stale-still-v1','stale-still',1,9100,"
                "'https://example.test/stale.png','old still','9:16','done',1)"
            )
            conn.commit()

        regenerated = short_drama_conversation.regenerate_shot(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "conversation_revision": edited["conversation"]["revision"],
                "version_id": edited["current_script"]["id"], "shot_key": shot_key,
            }, "scene-invalidation-regenerate",
        )
        self.assertIsNone(self.locked_scene_reference(shot_key))
        with closing(self.db()) as conn:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT locked FROM short_drama_assets WHERE id='stale-still'"
                ).fetchone()[0],
            )

        short_drama_asset_graph.sync_foundation(
            self.db, "alice", "alice", self.project["id"],
        )
        scenes = short_drama_asset_graph.scene_workspace(
            self.db, "alice", self.project["id"],
        )
        regenerated_scene = next(
            scene for scene in scenes["scenes"]
            if any(shot["shot_key"] == shot_key for shot in scene["shots"])
        )
        self.set_and_lock_scene_reference(
            scenes, regenerated_scene["scene_key"], "regenerated reference",
        )
        self.assertIsNotNone(self.locked_scene_reference(shot_key))

        restored = short_drama_conversation.restore_version(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "conversation_revision": regenerated["conversation"]["revision"],
                "version_id": edited["current_script"]["id"],
            }, "scene-invalidation-restore",
        )
        self.assertIsNone(self.locked_scene_reference(shot_key))
        short_drama_asset_graph.sync_foundation(
            self.db, "alice", "alice", self.project["id"],
        )
        scenes = short_drama_asset_graph.scene_workspace(
            self.db, "alice", self.project["id"],
        )
        restored_scene = next(
            scene for scene in scenes["scenes"]
            if any(shot["shot_key"] == shot_key for shot in scene["shots"])
        )
        self.set_and_lock_scene_reference(
            scenes, restored_scene["scene_key"], "restored reference",
        )
        self.assertIsNotNone(self.locked_scene_reference(shot_key))

        short_drama_conversation.generate_script(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "conversation_revision": restored["conversation"]["revision"],
                "instruction": "regenerate the complete script with a new scene plan",
            }, "scene-invalidation-whole-script",
        )
        self.assertIsNone(self.locked_scene_reference(shot_key))

    def test_delete_restore_without_intermediate_sync_does_not_revive_scene_reference(self):
        confirmed = self.confirm_direction(self.project["id"], 1, "delete-restore-scene")
        generated = short_drama_conversation.generate_script(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
            }, "delete-restore-scene-generate",
        )
        original = generated["current_script"]
        shot_key = original["script"]["shots"][0]["shot_key"]
        short_drama_asset_graph.sync_foundation(
            self.db, "alice", "alice", self.project["id"],
        )
        scenes = short_drama_asset_graph.scene_workspace(
            self.db, "alice", self.project["id"],
        )
        scene = next(
            item for item in scenes["scenes"]
            if any(shot["shot_key"] == shot_key for shot in item["shots"])
        )
        self.set_and_lock_scene_reference(scenes, scene["scene_key"], "deleted reference")
        self.assertIsNotNone(self.locked_scene_reference(shot_key))

        deleted = short_drama_conversation.change_shot_structure(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "conversation_revision": generated["conversation"]["revision"],
                "version_id": original["id"], "shot_key": shot_key,
                "action": "delete", "instruction": "remove this shot",
            }, "delete-restore-scene-delete",
        )
        self.assertIsNone(self.locked_scene_reference(shot_key))
        short_drama_conversation.restore_version(
            self.db, "alice", "alice", {
                "project_id": self.project["id"],
                "conversation_revision": deleted["conversation"]["revision"],
                "version_id": original["id"],
            }, "delete-restore-scene-restore",
        )
        self.assertIsNone(self.locked_scene_reference(shot_key))

        short_drama_asset_graph.sync_foundation(
            self.db, "alice", "alice", self.project["id"],
        )
        self.assertIsNone(self.locked_scene_reference(shot_key))

    def test_idempotency_and_revision_conflicts_are_explicit(self):
        body = {
            "project_id": self.project["id"],
            "conversation_revision": 1,
            "message": "做成轻喜剧。",
        }
        first = short_drama_conversation.send_message(
            self.db, "alice", "alice", body, "same-key"
        )
        replay = short_drama_conversation.send_message(
            self.db, "alice", "alice", body, "same-key"
        )
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["conversation"], replay["conversation"])

        changed = dict(body, message="改成悬疑")
        with self.assertRaises(short_drama_conversation.ConversationError) as conflict:
            short_drama_conversation.send_message(
                self.db, "alice", "alice", changed, "same-key"
            )
        self.assertEqual("idempotency_conflict", conflict.exception.code)

        with self.assertRaises(short_drama_conversation.ConversationError) as stale:
            short_drama_conversation.send_message(
                self.db, "alice", "alice", changed, "new-key"
            )
        self.assertEqual("conversation_revision_conflict", stale.exception.code)

    def test_creative_advisor_recommends_tracks_selection_and_confirms_direction(self):
        hello = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": 1,
                "message": "你好",
            },
            "advisor-hello",
        )
        self.assertEqual("discovering", hello["conversation"]["understanding"]["phase"])
        self.assertIn("帮我推荐三个方向", hello["messages"][-1]["metadata"]["quick_replies"])

        recommended = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": hello["conversation"]["revision"],
                "message": "我想做一部母女短剧，你先给我三个不同的剧情方向",
            },
            "advisor-recommend",
        )
        options = recommended["messages"][-1]["metadata"]["recommendations"]
        self.assertEqual(3, len(options))
        self.assertIn("做一部母女短剧", recommended["conversation"]["understanding"]["creative_brief"])
        self.assertNotIn("给我三个", recommended["conversation"]["understanding"]["creative_brief"])
        self.assertEqual(
            ["emotion", "twist", "growth"], [item["id"] for item in options]
        )

        selected = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": recommended["conversation"]["revision"],
                "message": "方案二 · 冲突反转",
            },
            "advisor-select",
        )
        self.assertEqual(
            "twist",
            selected["conversation"]["understanding"]["selected_recommendation_id"],
        )
        self.assertEqual(
            "recommendation_selected", selected["messages"][-1]["metadata"]["kind"]
        )

        refined = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": selected["conversation"]["revision"],
                "message": "结尾再温暖一点",
            },
            "advisor-refine",
        )
        self.assertIn(
            "结尾再温暖一点",
            refined["conversation"]["understanding"]["creative_brief"],
        )
        self.assertIn("加入方案二", refined["messages"][-1]["content"])

        confirmed = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": refined["conversation"]["revision"],
                "message": "确认这个方向",
            },
            "advisor-confirm",
        )
        self.assertTrue(
            confirmed["conversation"]["understanding"]["direction_confirmed"]
        )
        self.assertTrue(confirmed["conversation"]["understanding"]["ready_to_generate"])
        self.assertEqual(
            "direction_confirmed", confirmed["messages"][-1]["metadata"]["kind"]
        )

        changed = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
                "message": "把结尾改成开放式，但保留温暖感",
            },
            "advisor-change-after-confirm",
        )
        self.assertFalse(changed["conversation"]["understanding"]["direction_confirmed"])
        self.assertTrue(changed["conversation"]["understanding"]["confirmation_invalidated"])
        self.assertEqual("refining", changed["conversation"]["understanding"]["phase"])
        with self.assertRaises(short_drama_conversation.ConversationError) as blocked:
            short_drama_conversation.generate_script(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "conversation_revision": changed["conversation"]["revision"],
                },
                "advisor-generate-before-reconfirm",
            )
        self.assertEqual("direction_confirmation_required", blocked.exception.code)

    def test_chat_questions_are_not_copied_into_repeated_script_dialogue(self):
        discussed = short_drama_conversation.send_message(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": 1,
                "message": "你好，具体的剧情是怎么样的？你的推荐呢？",
            },
            "script-chat",
        )
        confirmed = self.confirm_direction(
            self.project["id"], discussed["conversation"]["revision"], "script-chat"
        )
        generated = short_drama_conversation.generate_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
            },
            "script-after-chat",
        )
        script = generated["current_script"]["script"]
        dialogue = [item["text"] for item in script["dialogue_lines"]]
        spoken_dialogue = [item for item in dialogue if item]
        visuals = [item["visual"] for item in script["shots"]]
        self.assertEqual(len(spoken_dialogue), len(set(spoken_dialogue)))
        self.assertGreater(len(set(visuals)), 3)
        self.assertFalse(any("你的推荐" in item for item in dialogue))
        self.assertFalse(any("剧情是怎么样" in item for item in dialogue))
        self.assertIn("两位旧友在雨夜重逢", script["overview"]["logline"])
        self.assertEqual(
            "conversation-storyboard-v5",
            generated["current_script"]["model_version"],
        )
        self.assertEqual(
            "short-drama-conversation-script-v4", script["schema_version"]
        )
        self.assertTrue(
            all(item["provider_prompt"] for item in script["shots"])
        )
        self.assertEqual("pass", script["quality_gate"]["status"])
        self.assertEqual(6, len(script["story_beats"]))

    def test_long_quoted_planning_summary_is_fitted_to_the_shot_duration(self):
        script = short_drama_conversation.short_drama_storyboard.compile_storyboard(
            self.project,
            [
                "围绕“雨天被困便利店的女孩发愁无法回家，路过的外卖小哥主动将备用雨衣赠予她，赠予一份突如其来的温暖”展开故事"
            ],
            [
                {
                    "character_key": "girl",
                    "name": "女孩",
                    "identity": "被雨困住的女孩",
                    "personality": "敏感",
                },
                {
                    "character_key": "rider",
                    "name": "外卖小哥",
                    "identity": "路过的外卖员",
                    "personality": "热心",
                },
            ],
        )
        self.assertEqual("pass", script["quality_gate"]["status"])
        lines = {item["id"]: item for item in script["dialogue_lines"]}
        for shot in script["shots"]:
            line = lines[shot["dialogue_line_ids"][0]]
            self.assertLessEqual(
                line["estimated_reading_seconds"], shot["duration_seconds"]
            )

    def test_story_specific_script_does_not_fall_back_to_generic_mystery_template(self):
        mother_daughter = short_drama.create_project(
            self.db,
            "alice",
            payload(
                title="查分",
                synopsis=(
                    "凌晨查分，女儿高考失利，比估分低了40分。"
                    "她笑着宣布复读，回房才掉泪。"
                    "深夜撕掉大学照片，只留下本省师范，想离家近照顾母亲。"
                    "母亲看到字条和自己的复诊单，决定支持女儿重新选择。"
                ),
            ),
        )
        confirmed = self.confirm_direction(
            mother_daughter["id"], 1, "story-specific"
        )
        generated = short_drama_conversation.generate_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": mother_daughter["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
                "instruction": "结尾温暖克制",
            },
            "story-specific-script",
        )
        script = generated["current_script"]["script"]
        rendered = " ".join(
            [item["visual"] for item in script["shots"]]
            + [item["text"] for item in script["dialogue_lines"]]
        )
        self.assertIn("高考失利", rendered)
        self.assertIn("母亲", [item["name"] for item in script["characters"]])
        self.assertIn("女儿", [item["name"] for item in script["characters"]])
        self.assertNotIn("查清真相", rendered)
        self.assertNotIn("不该出现的线索", rendered)

    def test_single_shot_edits_append_auditable_versions(self):
        confirmed = self.confirm_direction(
            self.project["id"], 1, "generate-editable"
        )
        generated = short_drama_conversation.generate_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
            },
            "generate-editable",
        )
        first = generated["current_script"]
        first_script = first["script"]
        first_shot = first_script["shots"][0]
        original_total = sum(
            item["duration_seconds"] for item in first_script["shots"]
        )
        character = first_script["characters"][0]
        edited = short_drama_conversation.update_shot(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": generated["conversation"]["revision"],
                "version_id": first["id"],
                "shot_key": first_shot["shot_key"],
                "changes": {
                    "purpose": "用成绩页面建立核心冲突",
                    "visual": "清晨卧室，角色盯着成绩页面，手指停在鼠标上",
                    "camera": "运" * 300,
                    "continuity": "续" * 360,
                    "sound_design": "声" * 600,
                    "dialogue": {
                        "kind": "dialogue",
                        "character_key": character["character_key"],
                        "text": "我看到了。",
                        "speech_rate": 2.0,
                    },
                    "provider_prompt": "电影感写实，清晨卧室，角色盯着成绩页面。",
                },
            },
            "edit-shot-1",
        )
        self.assertNotEqual(first["id"], edited["current_script"]["id"])
        self.assertEqual(first["version"] + 1, edited["current_script"]["version"])
        edited_script = edited["current_script"]["script"]
        self.assertEqual(
            original_total,
            sum(item["duration_seconds"] for item in edited_script["shots"]),
        )
        self.assertEqual(
            "用成绩页面建立核心冲突",
            edited_script["shots"][0]["purpose"],
        )
        self.assertEqual(300, len(edited_script["shots"][0]["camera"]))
        self.assertEqual(360, len(edited_script["shots"][0]["continuity"]))
        self.assertEqual(600, len(edited_script["shots"][0]["sound_design"]))
        self.assertEqual(2.0, edited_script["dialogue_lines"][0]["speech_rate"])
        self.assertEqual(
            short_drama_storyboard._reading_seconds(
                edited_script["dialogue_lines"][0]
            ),
            edited_script["dialogue_lines"][0]["estimated_reading_seconds"],
        )

        conn = self.db()
        try:
            self.assertEqual(
                2,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_script_snapshots WHERE project_id=?",
                    (self.project["id"],),
                ).fetchone()[0],
            )
            stored_first = json.loads(conn.execute(
                "SELECT script_json FROM short_drama_script_snapshots WHERE id=?",
                (first["id"],),
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(first_script, stored_first)

        regenerated = short_drama_conversation.regenerate_shot(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": edited["conversation"]["revision"],
                "version_id": edited["current_script"]["id"],
                "shot_key": first_shot["shot_key"],
                "instruction": "保持剧情，只调整构图",
            },
            "regenerate-with-sound-design",
        )
        self.assertEqual(
            "声" * 600,
            regenerated["current_script"]["script"]["shots"][0]["sound_design"],
        )

        locked = short_drama_conversation.set_shot_lock(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": regenerated["conversation"]["revision"],
                "version_id": regenerated["current_script"]["id"],
                "shot_key": first_shot["shot_key"],
                "locked": True,
            },
            "lock-shot-1",
        )
        self.assertNotEqual(edited["current_script"]["id"], locked["current_script"]["id"])
        self.assertEqual(
            regenerated["current_script"]["version"] + 1,
            locked["current_script"]["version"],
        )
        self.assertTrue(locked["current_script"]["script"]["shots"][0]["locked"])
        with self.assertRaises(short_drama_conversation.ConversationError) as blocked:
            short_drama_conversation.regenerate_shot(
                self.db,
                "alice",
                "alice",
                {
                    "project_id": self.project["id"],
                    "conversation_revision": locked["conversation"]["revision"],
                    "version_id": locked["current_script"]["id"],
                    "shot_key": first_shot["shot_key"],
                    "instruction": "改成雨夜",
                },
                "regenerate-locked-shot",
            )
        self.assertEqual("shot_locked", blocked.exception.code)

    def test_sound_design_rejects_more_than_600_characters(self):
        characters = [{
            "character_key": "lead", "name": "林夏", "role_type": "main",
            "identity": "主角", "personality": "坚定",
        }]
        script = short_drama_storyboard.compile_storyboard(
            payload(shot_count=3, target_duration=15),
            ["相遇", "冲突", "和解"], characters,
        )
        with self.assertRaises(short_drama_conversation.ConversationError) as raised:
            short_drama_conversation._apply_shot_patch(
                script, script["shots"][0]["shot_key"],
                {"sound_design": "声" * 601},
            )
        self.assertEqual("shot_field_too_long", raised.exception.code)

    def test_locked_snapshot_does_not_mutate_legacy_production_tables(self):
        confirmed = self.confirm_direction(
            self.project["id"], 1, "generate-legacy-check"
        )
        generated = short_drama_conversation.generate_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": confirmed["conversation"]["revision"],
            },
            "generate",
        )
        short_drama_conversation.lock_script(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": generated["conversation"]["revision"],
                "version_id": generated["current_script"]["id"],
            },
            "lock",
        )
        conn = self.db()
        try:
            self.assertEqual(
                0,
                conn.execute(
                    "SELECT COUNT(*) FROM short_drama_scripts WHERE project_id=?",
                    (self.project["id"],),
                ).fetchone()[0],
            )
            self.assertEqual(
                "draft",
                conn.execute(
                    "SELECT stage FROM short_drama_projects WHERE id=?",
                    (self.project["id"],),
                ).fetchone()[0],
            )
        finally:
            conn.close()

    def test_http_routes_apply_auth_access_and_error_contracts(self):
        verify = lambda token: (
            {"username": token, "must_change": False} if token else None
        )
        anonymous = Handler(
            "/api/gen/short-drama/conversation?project_id=" + self.project["id"],
            token="",
        )
        self.assertTrue(short_drama.dispatch_http(anonymous, "GET", self.db, verify))
        self.assertEqual(401, anonymous.response[0])

        workspace = Handler(
            "/api/gen/short-drama/conversation?project_id=" + self.project["id"]
        )
        self.assertTrue(short_drama.dispatch_http(workspace, "GET", self.db, verify))
        self.assertEqual(200, workspace.response[0])

        message = Handler(
            "/api/gen/short-drama/conversation/messages",
            body={
                "project_id": self.project["id"],
                "conversation_revision": 1,
                "message": "请突出人物选择。",
            },
        )
        self.assertTrue(short_drama.dispatch_http(message, "POST", self.db, verify))
        self.assertEqual(200, message.response[0])

        stale = Handler(
            "/api/gen/short-drama/conversation/messages",
            body={
                "project_id": self.project["id"],
                "conversation_revision": 1,
                "message": "这是过期页面提交。",
            },
            idempotency_key="stale-key-123",
        )
        short_drama.dispatch_http(stale, "POST", self.db, verify)
        self.assertEqual(409, stale.response[0])
        self.assertEqual("conversation_revision_conflict", stale.response[1]["code"])


if __name__ == "__main__":
    unittest.main()
