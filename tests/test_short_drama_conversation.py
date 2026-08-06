import sqlite3
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama, short_drama_conversation


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
        "visual_style": "电影感写实",
        "characters": ["林夏", "周野"],
        "beats": beats,
        "shots": shots,
    }


class ShortDramaConversationTests(unittest.TestCase):
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
            "conversation-storyboard-v4",
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

    def test_single_shot_edit_lock_and_regenerate_create_auditable_versions(self):
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
                    "duration_seconds": first_shot["duration_seconds"] + 1,
                    "dialogue": {
                        "kind": "dialogue",
                        "character_key": character["character_key"],
                        "text": "我看到了。",
                    },
                    "provider_prompt": "电影感写实，清晨卧室，角色盯着成绩页面。",
                },
            },
            "edit-shot-1",
        )
        self.assertEqual(2, edited["current_script"]["version"])
        edited_script = edited["current_script"]["script"]
        self.assertEqual(
            original_total,
            sum(item["duration_seconds"] for item in edited_script["shots"]),
        )
        self.assertEqual(
            "用成绩页面建立核心冲突",
            edited_script["shots"][0]["purpose"],
        )

        locked = short_drama_conversation.set_shot_lock(
            self.db,
            "alice",
            "alice",
            {
                "project_id": self.project["id"],
                "conversation_revision": edited["conversation"]["revision"],
                "version_id": edited["current_script"]["id"],
                "shot_key": first_shot["shot_key"],
                "locked": True,
            },
            "lock-shot-1",
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
