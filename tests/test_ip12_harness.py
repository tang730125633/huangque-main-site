import sys
import unittest
from pathlib import Path


HERMES = Path(__file__).parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import ip12_harness as harness


def decision(state, *, kind="propose_checkpoint", reply="这是当前结果", draft="可确认草稿"):
    updates = []
    if kind == "propose_checkpoint" and state["current_module"] == 5 and state["module_step"] == 1:
        titles = ["第 %02d 个真实选题" % index for index in range(1, 31)]
        draft = "\n".join("%d. %s" % (index, title) for index, title in enumerate(titles, 1))
        updates = [{
            "field": "topic_%d_%02d" % (((index - 1) // 10) + 1, ((index - 1) % 10) + 1),
            "value": title,
            "kind": "ai_option",
            "evidence_quote": "用户原话",
        } for index, title in enumerate(titles, 1)]
    return {
        "decision": kind,
        "checkpoint": state["module_step"] + 1 if kind == "propose_checkpoint" else 0,
        "reply": reply,
        "draft": draft if kind == "propose_checkpoint" else "",
        "self_review": "资料来源清楚，仍需本人确认。" if kind == "propose_checkpoint" else "",
        "profile_updates": updates,
        "confidence": 0.9,
    }


def intake_decision(*, kind="propose_checkpoint", reply="这是我整理的基础资料", draft="基础资料核对稿", updates=None):
    has_draft = kind in {"propose_checkpoint", "revise_intake"}
    return {
        "decision": kind,
        "checkpoint": 1 if kind == "propose_checkpoint" else 0,
        "reply": reply,
        "draft": draft if has_draft else "",
        "self_review": "只整理了用户原话，仍需本人确认。" if has_draft else "",
        "profile_updates": updates or [],
        "confidence": 0.9,
    }


class IP12HarnessTests(unittest.TestCase):
    def test_production_recommendations_stay_bounded_by_capability_family(self):
        expected = {
            "image": "image-generate",
            "audio": "audio-generate",
            "video": "digital-ip-text-generate",
            "canvas": "canvas-ops",
        }
        for family, action in expected.items():
            with self.subTest(family=family):
                recommendation = harness.production_recommendation(family)
                self.assertEqual(recommendation["capability_family"], family)
                self.assertEqual(recommendation["recommended_action"], action)
                self.assertIn(action, recommendation["candidate_actions"])
        with self.assertRaises(harness.HarnessError):
            harness.production_recommendation("video", "image-generate")
        with self.assertRaises(harness.HarnessError):
            harness.production_recommendation("unknown")

    def complete_intake(self):
        state = harness.initial_state()
        evidence = "我叫泽龙，22岁，在广州做 FDE，主要提供技术服务。"
        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(
                draft="称呼：泽龙；年龄：22岁；城市：广州；职业：FDE；收入来源：技术服务。",
                updates=[{
                    "field": "preferred_name",
                    "value": "泽龙",
                    "kind": "user_fact",
                    "evidence_quote": "我叫泽龙",
                }],
            ),
            evidence,
        )
        action = harness.available_actions(state)[0]
        state, event = harness.apply_action(state, action, state["revision"])
        self.assertIn("基础信息已确认", event["assistant_prefix"])
        self.assertEqual(state["ip_profile"]["facts"]["preferred_name"]["value"], "泽龙")
        return state

    def confirm_checkpoint(self, state):
        state, _, _ = harness.apply_model_decision(state, decision(state), "用户原话", pending_id="pending-1")
        action = harness.available_actions(state)[0]
        return harness.apply_action(state, action, state["revision"])

    def test_intake_supports_open_ended_multi_turn_trajectory(self):
        state = harness.initial_state()
        original_revision = state["revision"]

        state, _, reply = harness.apply_intake_decision(
            state,
            intake_decision(kind="answer_only", reply="手机号完全可以不填。"),
            "手机号必须填吗？",
        )
        self.assertEqual(state["intake"]["status"], "collecting")
        self.assertIn("可以不填", reply)

        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(kind="ask_follow_up", reply="你现在主要从事什么工作？"),
            "我叫泽龙，22岁，在广州。",
        )
        self.assertEqual(state["intake"]["status"], "collecting")

        first_draft = "称呼：泽龙；年龄：22岁；城市：广州；职业：FDE。"
        state, _, reply = harness.apply_intake_decision(
            state,
            intake_decision(draft=first_draft),
            "我现在做 FDE。",
        )
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        self.assertEqual(state["intake"]["draft"], first_draft)
        self.assertIn(first_draft, reply)
        self.assertEqual(state["current_module"], 1)
        self.assertEqual(state["module_step"], 0)
        self.assertGreater(state["revision"], original_revision)

        state, _, reply = harness.apply_intake_decision(
            state,
            intake_decision(kind="answer_only", reply="收入区间是可选项，不提供也不影响诊断。"),
            "收入一定要说吗？",
        )
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        self.assertEqual(state["intake"]["draft"], first_draft)
        self.assertIn("可选", reply)

        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(kind="ask_follow_up", reply="这段经历发生在当前工作之前吗？"),
            "我以前还做过健身教练。",
        )
        self.assertEqual(state["intake"]["status"], "editing")
        self.assertEqual(harness.available_actions(state), [])

        revised_draft = first_draft + "；过往经历：曾做健身教练，后来转向计算机方向。"
        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(draft=revised_draft),
            "对，在做 FDE 之前。",
        )
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        self.assertEqual(state["intake"]["draft"], revised_draft)
        self.assertEqual(state["current_module"], 1)

    def test_intake_drops_acronym_expansions_missing_from_user_evidence(self):
        evidence = "我在广州做 FDE，主要负责 Agent 智能体开发。"
        state, normalized, reply = harness.apply_intake_decision(
            harness.initial_state(),
            intake_decision(
                reply="你现在做 FDE（Front-end Development Engineering）。",
                draft="当前职业：FDE（Front-end Development Engineering）。",
                updates=[{
                    "field": "current_role",
                    "value": "FDE（Front-end Development Engineering）",
                    "kind": "user_fact",
                    "evidence_quote": "FDE",
                }],
            ),
            evidence,
        )

        self.assertNotIn("Front-end Development Engineering", reply)
        self.assertEqual(normalized["profile_updates"][0]["value"], "FDE")
        self.assertEqual(state["intake"]["draft"], "当前职业：FDE。")

    def test_intake_revision_drops_unsupported_update_without_false_failure(self):
        state, _, _ = harness.apply_intake_decision(
            harness.initial_state(),
            intake_decision(draft="过往经历：修车。"),
            "我以前修过车",
        )
        edit = next(action for action in harness.available_actions(state) if action["type"] == "edit_intake")
        state, _ = harness.apply_action(state, edit, state["revision"])
        revised = intake_decision(
            reply="已加入你补充的洗车经历。",
            draft="过往经历：修车、洗车。",
            updates=[{
                "field": "prior_roles",
                "value": "修车、洗车",
                "kind": "user_fact",
                "evidence_quote": "模型改写后并不存在于用户原话里的句子",
            }],
        )

        state, result, reply = harness.apply_intake_decision(state, revised, "我还洗过车")

        self.assertEqual(result["profile_updates"], [])
        self.assertEqual(state["intake"]["draft"], "过往经历：修车、洗车。")
        self.assertIn("已加入你补充的洗车经历", reply)

    def test_intake_rejects_repeated_optional_follow_up(self):
        state = harness.initial_state()
        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(kind="ask_follow_up", reply="方便的话，请告诉我你的年龄段？不想回答也可以跳过。"),
            "我目前在广州做 Agent 开发。",
        )

        with self.assertRaisesRegex(harness.HarnessError, "重复追问"):
            harness.apply_intake_decision(
                state,
                intake_decision(kind="ask_follow_up", reply="为了补齐资料，你大概属于哪个年龄段？"),
                "我以前修过车，后来转行了。",
            )

        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(kind="ask_follow_up", reply="你目前主要的收入来源是什么？不方便也可以跳过。"),
            "我以前修过车，后来转行了。",
        )
        self.assertEqual(state["intake"]["asked_follow_ups"], ["age", "income"])

    def test_intake_recovers_model_drift_and_keeps_latest_unconfirmed_facts(self):
        state = harness.initial_state()
        first = intake_decision(
            kind="ask_follow_up",
            reply="好的，泽龙。你现在主要从事什么工作？",
            updates=[{
                "field": "preferred_name",
                "value": "泽龙",
                "kind": "user_fact",
                "evidence_quote": "叫我泽龙就好",
            }],
        )
        first.update(checkpoint=1, draft="称呼：泽龙", self_review="仍需本人确认。")

        state, normalized, _ = harness.apply_intake_decision(state, first, "叫我泽龙就好")

        self.assertEqual(normalized["checkpoint"], 0)
        self.assertEqual(normalized["draft"], "")
        self.assertEqual(state["intake"]["status"], "collecting")
        self.assertEqual(state["intake"]["profile_updates"][0]["value"], "泽龙")
        self.assertNotIn("preferred_name", state["ip_profile"]["facts"])

        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(
                kind="ask_follow_up",
                reply="收到，叫你阿龙。你目前在哪座城市？",
                updates=[{
                    "field": "preferred_name",
                    "value": "阿龙",
                    "kind": "user_preference",
                    "evidence_quote": "还是叫我阿龙吧",
                }],
            ),
            "还是叫我阿龙吧",
        )
        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(
                draft="称呼：阿龙；城市：广州。",
                updates=[
                    {
                        "field": "preferred_name",
                        "value": "阿龙",
                        "kind": "user_preference",
                        "evidence_quote": "还是叫我阿龙吧",
                    },
                    {
                        "field": "city",
                        "value": "广州",
                        "kind": "user_fact",
                        "evidence_quote": "我在广州",
                    },
                ],
            ),
            "我在广州",
        )

        self.assertEqual(
            [(item["field"], item["value"]) for item in state["intake"]["profile_updates"]],
            [("preferred_name", "阿龙"), ("city", "广州")],
        )
        action = harness.available_actions(state)[0]
        state, _ = harness.apply_action(state, action, state["revision"])
        self.assertEqual(state["ip_profile"]["preferences"]["preferred_name"]["value"], "阿龙")
        self.assertEqual(state["ip_profile"]["facts"]["city"]["value"], "广州")

    def test_final_intake_proposal_can_remove_a_withdrawn_partial_fact(self):
        state, _, _ = harness.apply_intake_decision(
            harness.initial_state(),
            intake_decision(
                kind="ask_follow_up",
                reply="收到。你希望我怎么称呼你？",
                updates=[{
                    "field": "age",
                    "value": "22",
                    "kind": "user_fact",
                    "evidence_quote": "我今年 22 岁",
                }],
            ),
            "我今年 22 岁",
        )
        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(
                draft="称呼：泽龙；年龄：不记录。",
                updates=[{
                    "field": "preferred_name",
                    "value": "泽龙",
                    "kind": "user_fact",
                    "evidence_quote": "叫我泽龙",
                }],
            ),
            "叫我泽龙，年龄不要记录",
        )

        self.assertEqual([item["field"] for item in state["intake"]["profile_updates"]], ["preferred_name"])
        action = harness.available_actions(state)[0]
        state, _ = harness.apply_action(state, action, state["revision"])
        self.assertNotIn("age", state["ip_profile"]["facts"])

    def test_initial_intake_revise_decision_becomes_a_confirmation_proposal(self):
        state, decision_result, _ = harness.apply_intake_decision(
            harness.initial_state(),
            intake_decision(kind="revise_intake", draft="称呼：泽龙。"),
            "叫我泽龙",
        )
        self.assertEqual(decision_result["decision"], "propose_checkpoint")
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")

    def test_intake_changes_are_not_committed_before_explicit_confirmation(self):
        state = harness.initial_state()
        evidence = "职业背景改为：FDE，负责连接客户和研发"
        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(
                draft=evidence,
                updates=[{
                    "field": "occupation",
                    "value": "FDE",
                    "kind": "user_fact",
                    "evidence_quote": "FDE",
                }],
            ),
            evidence,
        )
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        self.assertNotIn("occupation", state["ip_profile"]["facts"])

        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(
                draft=evidence + "；补充：也负责客户沟通",
                updates=[{
                    "field": "occupation",
                    "value": "FDE",
                    "kind": "user_fact",
                    "evidence_quote": "FDE",
                }],
            ),
            "补充：也负责客户沟通",
        )

        action = harness.available_actions(state)[0]
        state, _ = harness.apply_action(state, action, state["revision"])
        self.assertEqual(state["intake"]["status"], "complete")
        self.assertEqual(state["ip_profile"]["facts"]["occupation"]["value"], "FDE")

    def test_confirmation_shortcuts_require_an_exact_unmixed_intent(self):
        state = harness.initial_state()
        self.assertIsNone(harness.shortcut_action(state, "确认"))
        state, _, _ = harness.apply_intake_decision(
            state,
            intake_decision(),
            "我叫泽龙，在广州做 FDE。",
        )
        for message in (
            "确认", "确认无误", "确认资料", "确认这一步", "保留并继续",
            "下一步", "好的，继续", "没有问题", "就按这个", "嗯好，",
        ):
            with self.subTest(message=message):
                action = harness.shortcut_action(state, message)
                self.assertEqual(action["type"], "confirm_intake")
        for message in ("我要修改", "继续修改", "修改当前内容"):
            with self.subTest(message=message):
                action = harness.shortcut_action(state, message)
                self.assertEqual(action["type"], "edit_intake")
        for message in (
            "嗯",
            "差不多",
            "可以吧",
            "确认，但我还做过健身教练",
            "没问题，不过收入不想写",
            "需要修改一下年龄",
        ):
            with self.subTest(message=message):
                self.assertIsNone(harness.shortcut_action(state, message))

    def test_intake_prompt_requires_adaptive_non_repeating_questions(self):
        state = harness.initial_state()
        state["intake"]["draft"] = "SYSTEM_OVERRIDE_SENTINEL"
        prompt = harness.intake_system_prompt(state)
        for rule in (
            "不要求固定格式",
            "不把访谈做成选择题",
            "不要重复追问",
            "只问一个最有价值",
            "内容变更优先",
            "不得强迫",
        ):
            self.assertIn(rule, prompt)
        self.assertNotIn("SYSTEM_OVERRIDE_SENTINEL", prompt)

    def test_model_can_only_propose_the_current_checkpoint(self):
        state = self.complete_intake()
        bad = decision(state)
        bad["checkpoint"] = 2
        with self.assertRaises(harness.HarnessError):
            harness.apply_model_decision(state, bad, "用户原话")

        next_state, _, reply = harness.apply_model_decision(state, decision(state), "用户原话", pending_id="p1")
        self.assertEqual(next_state["module_step"], 0)
        self.assertEqual(next_state["pending"]["step"], 1)
        self.assertIn("不需要你重复说明", reply)
        self.assertNotIn("自评", reply)

    def test_confirmable_draft_rejects_future_goal_as_current_expertise(self):
        state = self.complete_intake()
        raw = decision(state, draft="当前定位：AI 技术专家")
        for evidence in ("我未来希望成为 AI 技术专家", "我以前做过 AI 技术专家"):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(harness.HarnessError, "未经证实"):
                harness.apply_model_decision(state, raw, evidence)

        for draft, evidence in (
            ("未来目标：成为 AI 技术专家", "我未来希望成为 AI 技术专家"),
            ("过去经历：做过 AI 技术顾问", "我以前做过 AI 技术顾问"),
        ):
            with self.subTest(draft=draft):
                next_state, _, _ = harness.apply_model_decision(
                    state, decision(state, draft=draft), evidence, pending_id="grounded-timeline"
                )
                self.assertEqual(next_state["pending"]["draft"], draft)

        next_state, _, _ = harness.apply_model_decision(
            state, raw, "我目前的职业身份就是 AI 技术专家", pending_id="grounded-expert"
        )
        self.assertEqual(next_state["pending"]["draft"], raw["draft"])

    def test_module_five_topics_require_direct_user_evidence(self):
        state = self.complete_intake()
        state.update(current_module=5, module_step=1, completed_modules=[1, 2, 3, 4])
        state["foundation_report"] = {"status": "confirmed"}
        raw = decision(state)
        old_title = raw["profile_updates"][0]["value"]
        raw["profile_updates"][0].update(value="医疗行业真实案例", evidence_quote="用户原话")
        raw["draft"] = raw["draft"].replace(old_title, "医疗行业真实案例")
        with self.assertRaisesRegex(harness.HarnessError, "医疗"):
            harness.apply_model_decision(state, raw, "用户原话")

        raw["profile_updates"][0]["evidence_quote"] = "我亲自做过医疗行业真实案例"
        next_state, _, _ = harness.apply_model_decision(
            state, raw, "用户原话\n我亲自做过医疗行业真实案例", pending_id="grounded-topics"
        )
        self.assertEqual(len(next_state["pending"]["profile_updates"]), 30)
        edit = next(action for action in harness.available_actions(next_state) if action["type"] == "edit_checkpoint")
        next_state, _ = harness.apply_action(next_state, edit, next_state["revision"])
        next_state, _, _ = harness.apply_model_decision(
            next_state,
            decision(next_state, kind="ask_follow_up", reply="你希望先发布哪一个种类？"),
            "先从第一个种类开始",
        )
        self.assertEqual(len(next_state["pending"]["profile_updates"]), 30)

    def test_module_five_reports_all_unsupported_terms_in_one_retry(self):
        state = self.complete_intake()
        state.update(current_module=5, module_step=1, completed_modules=[1, 2, 3, 4])
        state["foundation_report"] = {"status": "confirmed"}
        raw = decision(state)
        replacements = ("医疗行业案例观察", "客户增长：哪些尝试成功，哪些失败")
        for index, title in enumerate(replacements):
            old_title = raw["profile_updates"][index]["value"]
            raw["profile_updates"][index].update(value=title, evidence_quote="用户原话")
            raw["draft"] = raw["draft"].replace(old_title, title)

        with self.assertRaises(harness.HarnessError) as caught:
            harness.apply_model_decision(state, raw, "用户原话")

        error = str(caught.exception)
        for term in ("医疗", "成功", "客户", "增长", "案例"):
            self.assertIn(term, error)

    def test_module_five_compiler_builds_protocol_fields_and_markdown(self):
        state = self.complete_intake()
        state.update(current_module=5, module_step=1, completed_modules=[1, 2, 3, 4])
        state["foundation_report"] = {"status": "confirmed"}
        names = ("转行经验分享", "智能体应用实践", "垂直行业真实验证")
        source = "\n".join("%d. **%s**" % (index, name) for index, name in enumerate(names, 1))
        state["ip_profile"]["confirmed_outputs"]["5-1"] = {"content": source}
        quote = "我只讲真实过程和待验证计划"
        raw = {
            "decision": "propose_checkpoint",
            "reply": "这是按三个已确认种类整理的选题。",
            "categories": [
                {
                    "name": name,
                    "topics": [
                        {"title": "%s：真实过程 %02d" % (name, index), "evidence_id": "E1"}
                        for index in range(1, 11)
                    ],
                }
                for name in names
            ],
            "self_review": "只使用了给定证据。",
            "confidence": 0.9,
        }
        raw["categories"][0]["topics"][0]["title"] = "医疗客户成功案例的效率提升"

        compiled = harness.compile_module_five_topics(
            raw, state, source + "\n" + quote, {"E1": quote}
        )

        self.assertEqual(compiled["checkpoint"], 2)
        self.assertEqual(len(compiled["profile_updates"]), 30)
        self.assertEqual(compiled["profile_updates"][0]["field"], "topic_1_01")
        self.assertEqual(compiled["profile_updates"][0]["value"], "垂直行业具体对象实践过程记录的待验证结果")
        self.assertEqual(compiled["profile_updates"][-1]["field"], "topic_3_10")
        self.assertIn("### 转行经验分享", compiled["draft"])
        raw["categories"][0]["name"] = "未经确认的新种类"
        with self.assertRaisesRegex(harness.HarnessError, "种类名称"):
            harness.compile_module_five_topics(
                raw, state, source + "\n" + quote, {"E1": quote}
            )

    def test_module_five_does_not_reask_audience_after_categories_are_confirmed(self):
        state = self.complete_intake()
        state.update(current_module=5, module_step=1, completed_modules=[1, 2, 3, 4])
        state["foundation_report"] = {"status": "confirmed"}
        state["ip_profile"]["confirmed_outputs"]["5-1"] = {
            "content": "1. AI Agent 搭建\n2. AI 需求拆解\n3. 跨行业学习"
        }
        raw = {
            "decision": "ask_follow_up",
            "reply": "请补充三类各自最想吸引的人群。",
            "categories": [],
            "self_review": "",
            "confidence": 0.8,
        }

        with self.assertRaisesRegex(harness.HarnessError, "必须直接生成完整 3×10"):
            harness.compile_module_five_topics(raw, state, "已有充分资料", {"E1": "已有充分资料"})

    def test_module_five_final_checkpoint_reuses_the_confirmed_3x10_without_a_model(self):
        state = self.complete_intake()
        state.update(current_module=5, module_step=2, completed_modules=[1, 2, 3, 4])
        state["foundation_report"] = {"status": "confirmed"}
        categories = ("转行经验分享", "智能体应用实践", "垂直行业真实验证")
        source = "\n\n".join(
            "### %s\n%s" % (
                name,
                "\n".join("%d. %s选题%02d" % (index, name, index) for index in range(1, 11)),
            )
            for name in categories
        )
        state["ip_profile"]["confirmed_outputs"]["5-2"] = {"content": source}

        compiled = harness.compile_module_five_confirmation(state)

        self.assertEqual(compiled["checkpoint"], 3)
        self.assertEqual(compiled["profile_updates"], [])
        self.assertIn("精选 3 个重点选题", compiled["draft"])
        self.assertIn("【转行经验分享】转行经验分享选题01", compiled["draft"])
        for message in ("继续", "下一步", "进入下一步", "好的，下一步！", "保留并继续"):
            with self.subTest(message=message):
                self.assertTrue(harness.is_continue_message(message))
        self.assertFalse(harness.is_continue_message("下一步会生成口播吗？"))
        self.assertTrue(harness.is_content_review_message("口播文案我先看看"))
        self.assertTrue(harness.is_content_review_message("把三篇完整文章给我看一下"))
        self.assertFalse(harness.is_content_review_message("下一步会生成口播吗？"))
        self.assertFalse(harness.is_content_review_message("这篇文案给我的感觉太正式，改口语一点"))

    def test_explicit_asset_requests_map_to_bounded_production_actions(self):
        cases = {
            "把这篇做成一张封面图片": ("image", "image-generate"),
            "请把当前口播生成音频": ("audio", "audio-generate"),
            "用 Grok 把这篇做成视频": ("video", "video-generate"),
            "把这篇放到 Canvas 画布": ("canvas", "canvas-ops"),
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                intent = harness.production_intent(message)
                self.assertEqual(
                    (intent["capability_family"], intent["recommended_action"]), expected
                )
        self.assertIsNone(harness.production_intent("视频和图片分别是什么格式？"))

    def test_module_six_checkpoints_reuse_one_generated_content_pack(self):
        state = self.complete_intake()
        state.update(current_module=6, module_step=1, completed_modules=[1, 2, 3, 4, 5])
        pack = {
            "kind": "content_pack_v1",
            "format": "featured_3_v1",
            "categories": [{
                "name": "种类%d" % category,
                "description": "这是种类%d中最值得优先发布的选题。" % category,
                "topics": [{
                    "title": "种类%d选题01" % category,
                    "objective": "建立信任",
                    "versions": [{"version": 1, "content": ("这是种类%d的完整口播正文，包含钩子、观点、解释和行动引导。" % category) * 8}],
                }],
            } for category in range(1, 4)],
        }

        review = harness.compile_module_six_checkpoint(state, pack)
        self.assertEqual(review["checkpoint"], 2)
        self.assertIn("种类1选题01", review["draft"])
        self.assertIn("这是种类1的完整口播正文", review["draft"])
        self.assertIn("这是种类3的完整口播正文", review["draft"])
        self.assertNotIn("你想先看哪一篇", review["reply"])

        state["module_step"] = 2
        confirmation = harness.compile_module_six_checkpoint(state, pack)
        self.assertEqual(confirmation["checkpoint"], 3)
        self.assertIn("3 篇完整文案", confirmation["draft"])

        pack["categories"][1]["topics"][0]["versions"][0]["content"] = "只有标题，不是完整正文。"
        with self.assertRaisesRegex(harness.HarnessError, "缺少完整文案"):
            harness.compile_module_six_checkpoint(state, pack)

    def test_module_six_style_reuses_confirmed_preferences(self):
        state = self.complete_intake()
        state.update(current_module=6, module_step=0, completed_modules=[1, 2, 3, 4, 5])
        evidence = "口播偏好是大白话、60 到 90 秒，结尾引导收藏或留言。"

        decision = harness.compile_module_six_style(state, evidence)

        self.assertEqual(decision["checkpoint"], 1)
        self.assertIn("60 到 90 秒", decision["draft"])
        self.assertEqual(decision["profile_updates"][0]["evidence_quote"], evidence)
        self.assertIsNone(harness.compile_module_six_style(state, "我还没有想好风格"))

        exact_words = (
            "1min。"
            "我希望口播是偏口语化的，就像和好友在聊天，"
            "我希望观众可以点赞、评论我。"
        )
        exact = harness.compile_module_six_style(state, exact_words)
        self.assertEqual(exact["checkpoint"], 1)
        self.assertIn("点赞、评论", exact["draft"])
        self.assertNotIn("；；", exact["draft"])

        state["pending"] = {
            "id": "module-six-style",
            "kind": "checkpoint",
            "status": "awaiting_confirmation",
            "module": 6,
            "step": 1,
            "draft": exact["draft"],
            "self_review": exact["self_review"],
            "profile_updates": exact["profile_updates"],
            "confidence": 1.0,
        }
        self.assertIsNone(
            harness.compile_module_six_style(
                state, exact_words + "把重复的分号改成单个，其他不变。"
            )
        )

    def test_semantically_duplicate_reply_does_not_repeat_the_draft(self):
        draft = (
            "泽龙目前在广州，从事 FDE（Front-end Development Engineering），实际工作内容是 "
            "Agent 智能体开发，已在这个行业工作约三个月。期间，他与同事研究如何将 AI "
            "赋能于垂直行业，并参与制作了许多产品和 demo。此前，泽龙做过修车、服务员和"
            "工厂生产岗位，后来因为对电脑更感兴趣而转行。目前主要收入来源包括这份工作，"
            "以及接一些项目。年龄或年龄段、具体收入区间目前没有记录。"
        )
        model_reply = (
            "我先把目前了解到的情况整理如下，请你看看是否准确：\n\n泽龙目前在广州，从事 FDE，"
            "实际工作内容是 Agent 智能体开发，已在这个行业工作约三个月。期间，他与同事研究"
            "如何将 AI 赋能于垂直行业，并参与制作了许多产品和 demo。\n\n此前，泽龙做过修车、"
            "服务员和工厂生产岗位，后来因为对电脑更感兴趣而转行。\n\n目前主要收入来源包括这份 "
            "FDE/Agent 智能体开发工作，以及接一些项目。年龄或年龄段、具体收入区间目前没有记录。"
            "\n\n以上内容准确吗？如果有需要补充或修改的地方，直接告诉我即可。"
        )
        reply = harness.render_model_reply({
            "decision": "propose_checkpoint",
            "reply": model_reply,
            "draft": draft,
        })
        self.assertEqual(reply.count(draft), 1)
        self.assertNotIn("我先把目前了解到的情况", reply)

        keyword_draft = """核心关键词：
1. AI
2. 智能体编程与编排
3. 人与 AI 的沟通连接
4. AI 学习与应用普及
5. AI 时代推动者
依据：以上关键词均来自用户已确认资料。"""
        keyword_reply = """我提炼出 5 个核心关键词，请核对：
1. AI
2. 智能体编程与编排
3. 人与 AI 的沟通连接
4. AI 学习与应用普及
5. AI 时代推动者
这些关键词包含当前实践和长期目标，请确认或调整。"""
        rendered = harness.render_model_reply({
            "decision": "propose_checkpoint", "reply": keyword_reply, "draft": keyword_draft,
        })
        self.assertEqual(rendered.count("1. AI"), 1)
        self.assertNotIn("我提炼出 5 个核心关键词", rendered)

    def test_substantive_confirmable_reply_repairs_empty_internal_fields(self):
        state = self.complete_intake()
        reply = "\n\n".join((
            "### 转行实践记录\n边界：只分享本人已经发生的转行过程、选择依据、阶段性反思和仍未解决的问题，不包装为成功案例。",
            "### 智能体工作方法\n边界：只展示本人实际使用智能体完成工作的输入、执行过程、结果和复盘，不声称适用于所有人。",
            "### 行业需求验证\n边界：只记录参与项目时亲自观察到的问题、尝试过的方法和仍待验证的判断，不虚构客户成绩。",
        ))
        raw = decision(state, reply=reply, draft="")
        raw["self_review"] = ""

        next_state, normalized, rendered = harness.apply_model_decision(
            state, raw, "用户希望分享真实过程，不包装成专家。"
        )

        self.assertEqual(normalized["draft"], reply)
        self.assertTrue(normalized["self_review"])
        self.assertEqual(next_state["pending"]["draft"], reply)
        self.assertEqual(rendered.count("### 转行实践记录"), 1)

        short = decision(state, reply="请核对。", draft="")
        short["self_review"] = ""
        with self.assertRaisesRegex(harness.HarnessError, "没有返回可确认内容"):
            harness.apply_model_decision(state, short, "用户原话")

    def test_model_words_never_complete_a_module(self):
        state = self.complete_intake()
        raw = decision(state, reply="✅ 模块 1 完成。接下来进入模块 2。")
        next_state, _, _ = harness.apply_model_decision(state, raw, "用户原话", pending_id="p1")
        self.assertEqual(next_state["current_module"], 1)
        self.assertEqual(next_state["completed_modules"], [])

    def test_questions_tangents_and_revisions_preserve_the_current_checkpoint(self):
        state = self.complete_intake()
        state, _, _ = harness.apply_model_decision(state, decision(state), "用户原话", pending_id="p1")
        original_draft = state["pending"]["draft"]

        for kind, reply in (
            ("answer_only", "这是对当前草稿的解释。"),
            ("answer_only", "我们先回应你的题外问题，再回来继续。"),
        ):
            with self.subTest(reply=reply):
                state, _, _ = harness.apply_model_decision(
                    state,
                    decision(state, kind=kind, reply=reply),
                    "用户原话",
                )
                self.assertEqual(state["module_step"], 0)
                self.assertEqual(state["pending"]["draft"], original_draft)

        revised = decision(state, draft="结合补充经历后的新草稿")
        state, _, _ = harness.apply_model_decision(state, revised, "用户原话", pending_id="p1-revised")
        self.assertEqual(state["module_step"], 0)
        self.assertEqual(state["pending"]["step"], 1)
        self.assertEqual(state["pending"]["draft"], "结合补充经历后的新草稿")

    def test_module_prompt_accepts_free_form_and_forbids_repeated_questions(self):
        prompt = harness.system_prompt(self.complete_intake())
        for rule in (
            "不要求固定格式",
            "不把访谈做成选择题",
            "不要重复追问",
            "一次一项或多项",
            "内容变更优先",
        ):
            self.assertIn(rule, prompt)

    def test_module_one_uses_stated_help_goal_as_value_basis(self):
        state = self.complete_intake()
        state["pending"] = {
            "id": "m1-collecting", "kind": "checkpoint", "status": "collecting",
            "module": 1, "step": 1, "profile_updates": [
                {"field": field} for field in (
                    "key_experience", "core_skills", "long_term_interest",
                    "target_audience", "audience_benefit",
                )
            ],
        }

        self.assertIn("帮助目标", harness.system_prompt(state))
        with self.assertRaisesRegex(harness.HarnessError, "必须直接提炼候选关键词"):
            harness.validate_model_decision(
                decision(state, kind="ask_follow_up", reply="你最看重哪两项价值观？"),
                state,
                "",
            )

    def test_module_two_reuses_completed_positioning_instead_of_asking_again(self):
        state = self.complete_intake()
        state.update(current_module=2, module_step=0, completed_modules=[1])
        state["ip_profile"]["confirmed_outputs"]["1-1"] = {"draft": "已确认定位关键词"}

        self.assertIn("模块 1 已确认", harness.system_prompt(state))
        with self.assertRaisesRegex(harness.HarnessError, "必须从已有经历、行为和价值观"):
            harness.validate_model_decision(
                decision(state, kind="ask_follow_up", reply="别人通常会怎么形容你？"),
                state,
                "",
            )

    def test_module_three_reuses_confirmed_values_instead_of_asking_again(self):
        state = self.complete_intake()
        state.update(current_module=3, module_step=0, completed_modules=[1, 2])
        state["ip_profile"]["confirmed_outputs"].update({
            "1-1": {"draft": "已确认定位"},
            "2-1": {"draft": "已确认人设"},
        })

        self.assertIn("复用已确认", harness.system_prompt(state))
        with self.assertRaisesRegex(harness.HarnessError, "必须直接提炼候选关键词"):
            harness.validate_model_decision(
                decision(state, kind="ask_follow_up", reply="你希望别人怎样感受你？"),
                state,
                "",
            )

    def test_module_four_reuses_confirmed_story_instead_of_asking_again(self):
        state = self.complete_intake()
        state.update(current_module=4, module_step=0, completed_modules=[1, 2, 3])
        state["ip_profile"]["confirmed_outputs"].update({
            "1-1": {"draft": "已确认定位"},
            "2-1": {"draft": "已确认人设"},
            "3-1": {"draft": "已确认价值主张"},
        })

        self.assertIn("复用已确认", harness.system_prompt(state))
        with self.assertRaisesRegex(harness.HarnessError, "必须直接提炼候选故事节点"):
            harness.validate_model_decision(
                decision(state, kind="ask_follow_up", reply="请再讲一个关键故事。"),
                state,
                "",
            )

    def test_confirmed_intake_supplement_reopens_intake_without_advancing_module(self):
        state = self.complete_intake()
        supplement = "我以前还做过健身教练，但是失败了，后来才转向计算机方向。"
        raw = intake_decision(
            kind="revise_intake",
            reply="我把这段经历补进基础资料，先请你重新核对。",
            draft="称呼：泽龙；职业：FDE；过往经历：曾做健身教练，后来转向计算机方向。",
            updates=[{
                "field": "previous_career",
                "value": "曾做健身教练，后来转向计算机方向",
                "kind": "user_fact",
                "evidence_quote": "我以前还做过健身教练",
            }],
        )

        state, _, reply = harness.apply_model_decision(state, raw, supplement)

        self.assertEqual((state["current_module"], state["module_step"]), (1, 0))
        self.assertEqual(state["completed_modules"], [])
        self.assertEqual(state["intake"]["status"], "awaiting_confirmation")
        self.assertEqual(state["intake"]["mode"], "revision")
        self.assertNotIn("previous_career", state["ip_profile"]["facts"])
        self.assertIn("当前模块不会自动推进", reply)

        actions = harness.available_actions(state)
        self.assertEqual([item["label"] for item in actions], ["确认补充", "继续修改"])
        action = actions[0]
        state, event = harness.apply_action(state, action, state["revision"])
        self.assertEqual(state["intake"]["status"], "complete")
        self.assertNotIn("mode", state["intake"])
        self.assertEqual((state["current_module"], state["module_step"]), (1, 0))
        self.assertEqual(
            state["ip_profile"]["facts"]["previous_career"]["value"],
            "曾做健身教练，后来转向计算机方向",
        )
        self.assertIn("基础信息补充已确认", event["assistant_prefix"])

    def test_confirmed_intake_supplement_discards_only_unconfirmed_module_draft(self):
        state = self.complete_intake()
        state, _, _ = harness.apply_model_decision(
            state, decision(state), "模块用户原话", pending_id="unconfirmed-module-draft"
        )
        raw = intake_decision(
            kind="revise_intake",
            draft="称呼：泽龙；职业：FDE；补充经历：做过健身教练。",
            updates=[{
                "field": "previous_career",
                "value": "做过健身教练",
                "kind": "user_fact",
                "evidence_quote": "做过健身教练",
            }],
        )

        state, _, _ = harness.apply_model_decision(state, raw, "补充：做过健身教练")

        self.assertIsNone(state["pending"])
        self.assertEqual((state["current_module"], state["module_step"]), (1, 0))

    def test_recovery_can_discard_only_the_unconfirmed_module_draft(self):
        state = self.complete_intake()
        state.update(
            current_module=5,
            module_step=1,
            completed_modules=[1, 2, 3, 4],
            foundation_report={"status": "confirmed"},
            pending={
                "id": "invalid-draft",
                "kind": "checkpoint",
                "status": "editing",
                "module": 5,
                "step": 2,
                "draft": "不合格的 3×10 旧稿",
                "self_review": "",
                "profile_updates": [],
                "confidence": 0,
            },
        )
        state["ip_profile"]["ai_selections"]["long_term_content_categories"] = {
            "value": "转行经验、智能体实践、垂直行业验证",
            "evidence_quote": "",
        }

        state, _, _ = harness.apply_model_decision(
            state,
            decision(state, kind="answer_only", reply="未确认草稿已经清除。"),
            "继续",
            discard_pending=True,
        )

        self.assertIsNone(state["pending"])
        self.assertEqual((state["current_module"], state["module_step"]), (5, 1))
        self.assertEqual(
            state["ip_profile"]["ai_selections"]["long_term_content_categories"]["value"],
            "转行经验、智能体实践、垂直行业验证",
        )

    def test_intake_revision_is_rejected_after_a_module_checkpoint_is_confirmed(self):
        state, _ = self.confirm_checkpoint(self.complete_intake())
        raw = intake_decision(kind="revise_intake", draft="修改后的基础资料")
        with self.assertRaisesRegex(harness.HarnessError, "新诊断"):
            harness.apply_model_decision(state, raw, "修改基础资料")

    def test_every_open_module_supports_follow_up_discussion_revision_and_confirmation(self):
        for module in harness.MODULE_WORKFLOWS:
            with self.subTest(module=module):
                state = self.complete_intake()
                state.update(
                    current_module=module,
                    module_step=0,
                    completed_modules=list(range(1, module)),
                )
                if module >= 5:
                    state["foundation_report"] = {"status": "confirmed"}
                state = harness.normalize_state(state)

                state, _, _ = harness.apply_model_decision(
                    state,
                    decision(state, kind="ask_follow_up", reply="只追问一个尚未回答的问题。"),
                    "用户给了部分信息",
                )
                self.assertEqual((state["current_module"], state["module_step"]), (module, 0))

                state, _, _ = harness.apply_model_decision(
                    state,
                    decision(state, draft="第一版草稿"),
                    "用户补齐了信息",
                    pending_id=f"m{module}-draft",
                )
                state, _, _ = harness.apply_model_decision(
                    state,
                    decision(state, kind="answer_only", reply="解释当前草稿，不推进。"),
                    "为什么这样整理？",
                )
                self.assertEqual(state["pending"]["draft"], "第一版草稿")

                edit = next(action for action in harness.available_actions(state) if action["type"] == "edit_checkpoint")
                state, _ = harness.apply_action(state, edit, state["revision"])
                state, _, _ = harness.apply_model_decision(
                    state,
                    decision(state, draft="按用户意见修改后的草稿"),
                    "请换一种表达",
                    pending_id=f"m{module}-revised",
                )
                confirm = next(action for action in harness.available_actions(state) if action["type"] == "confirm_checkpoint")
                state, _ = harness.apply_action(state, confirm, state["revision"])
                self.assertEqual(state["module_step"], 1)
                self.assertEqual(state["ip_profile"]["confirmed_outputs"][f"{module}-1"]["content"], "按用户意见修改后的草稿")

    def test_module_follow_up_keeps_unconfirmed_facts_until_checkpoint_confirmation(self):
        state = self.complete_intake()
        message = "直到转入 AI 行业之后，我开始帮助企业搭建 Agent。"
        raw = decision(state, kind="ask_follow_up", reply="这次转向后来怎样影响了你？")
        raw["profile_updates"] = [{
            "field": "turning_point",
            "value": "转入 AI 行业并开始帮助企业搭建 Agent",
            "kind": "user_fact",
            "evidence_quote": "直到转入 AI 行业之后",
        }]

        state, _, _ = harness.apply_model_decision(state, raw, message, pending_id="partial-1")

        self.assertEqual(state["pending"]["status"], "collecting")
        self.assertEqual(state["pending"]["profile_updates"][0]["field"], "turning_point")
        self.assertEqual(harness.available_actions(state), [])
        state = harness.normalize_state(state)
        final = decision(state, draft="关键转折：转入 AI 行业，并开始帮助企业搭建 Agent。")
        final["profile_updates"] = raw["profile_updates"]
        state, _, _ = harness.apply_model_decision(state, final, "这让我找到了愿意长期投入的方向。")
        action = harness.available_actions(state)[0]
        state, _ = harness.apply_action(state, action, state["revision"])
        self.assertEqual(state["ip_profile"]["facts"]["turning_point"]["value"], raw["profile_updates"][0]["value"])

    def test_confirm_action_advances_exactly_one_checkpoint(self):
        state = self.complete_intake()
        next_state, event = self.confirm_checkpoint(state)
        self.assertEqual(next_state["module_step"], 1)
        self.assertEqual(next_state["current_module"], 1)
        self.assertTrue(event["continue_model"])
        self.assertEqual(event["assistant_prefix"], "")
        self.assertIn("1-1", next_state["ip_profile"]["confirmed_outputs"])

    def test_stale_confirmation_is_rejected(self):
        state = self.complete_intake()
        state, _, _ = harness.apply_model_decision(state, decision(state), "用户原话", pending_id="p1")
        action = harness.available_actions(state)[0]
        with self.assertRaises(harness.HarnessConflict):
            harness.apply_action(state, action, state["revision"] - 1)

    def test_user_fact_requires_a_verbatim_quote(self):
        state = self.complete_intake()
        raw = decision(state)
        raw["profile_updates"] = [{
            "field": "occupation",
            "value": "FDE",
            "kind": "user_fact",
            "evidence_quote": "我是一名 FDE",
        }]
        with self.assertRaises(harness.HarnessError):
            harness.apply_model_decision(state, raw, "用户只说了别的内容")

    def test_follow_up_drops_unsupported_update_without_dropping_reply(self):
        state = self.complete_intake()
        raw = decision(state, kind="ask_follow_up", reply="请再说说你的核心技能。")
        raw["profile_updates"] = [{
            "field": "core_skill", "value": "AI Agent 搭建", "kind": "user_fact",
            "evidence_quote": "模型自己改写的句子",
        }]
        state, result, reply = harness.apply_model_decision(state, raw, "用户只说了另一句话")
        self.assertEqual(reply, "请再说说你的核心技能。")
        self.assertEqual(result["profile_updates"], [])
        self.assertIsNone(state["pending"])

    def test_pending_draft_revision_drops_unsupported_update_without_losing_draft(self):
        state = self.complete_intake()
        state, _, _ = harness.apply_model_decision(
            state, decision(state, draft="第一版人设画像"), "用户原话", pending_id="draft-1"
        )
        revised = decision(state, draft="加入长期职业理想后的完整人设画像")
        revised["profile_updates"] = [{
            "field": "career_goal",
            "value": "跨行业复制并放大影响力",
            "kind": "user_fact",
            "evidence_quote": "模型改写后并不存在于用户原话里的句子",
        }]

        state, result, reply = harness.apply_model_decision(
            state, revised, "我希望先深耕一个行业，再总结规律", pending_id="draft-2"
        )

        self.assertEqual(result["profile_updates"], [])
        self.assertEqual(state["pending"]["draft"], "加入长期职业理想后的完整人设画像")
        self.assertIn("加入长期职业理想后的完整人设画像", reply)

    def test_conflicting_duration_requires_one_clarification(self):
        state = self.complete_intake()
        state["ip_profile"]["facts"]["years_in_current_industry"] = {
            "value": "2年", "evidence_quote": "我做了2年",
        }
        result = harness.duration_conflict_decision(state, "我进入 AI 和 Agent 领域只有三个月")
        self.assertEqual(result["decision"], "ask_follow_up")
        self.assertIn("2年", result["reply"])
        self.assertIn("三个月", result["reply"])
        self.assertIsNone(harness.duration_conflict_decision(state, "整体从业2年，其中 AI 实践三个月"))

    def test_modules_five_and_six_only_show_pdf_badge_when_pdf_is_ready(self):
        templates = Path(__file__).parents[1] / "server" / "hermes_ip12" / "templates"
        for filename in ("index.html", "index_clean.html"):
            source = (templates / filename).read_text(encoding="utf-8")
            self.assertIn("foundation.status==='awaiting_confirmation'?'待确认 PDF'", source)
            self.assertIn("等待模块 1-4", source)

    def test_module_four_completion_waits_for_report_confirmation(self):
        state = self.complete_intake()
        state.update(current_module=4, module_step=3, completed_modules=[1, 2, 3])
        state, _, _ = harness.apply_model_decision(state, decision(state), "用户原话", pending_id="p4")
        action = harness.available_actions(state)[0]
        state, event = harness.apply_action(state, action, state["revision"])
        self.assertEqual(state["current_module"], 4)
        self.assertIn(4, state["completed_modules"])
        self.assertEqual(state["foundation_report"]["status"], "generating")
        self.assertFalse(event["continue_model"])

    def test_module_four_combines_story_summary_and_recommendation(self):
        checkpoints = harness.MODULE_WORKFLOWS[4]["checkpoints"]
        self.assertEqual(len(checkpoints), 4)
        self.assertIn("故事资产清单", checkpoints[-1])
        self.assertIn("推荐长期核心故事", checkpoints[-1])

    def test_module_six_is_the_open_flow_terminal(self):
        state = self.complete_intake()
        state.update(current_module=6, module_step=2, completed_modules=[1, 2, 3, 4, 5])
        state, _, _ = harness.apply_model_decision(state, decision(state), "用户原话", pending_id="p6")
        action = harness.available_actions(state)[0]
        state, _ = harness.apply_action(state, action, state["revision"])
        self.assertEqual(state["current_module"], 6)
        self.assertEqual(state["completed_modules"], [1, 2, 3, 4, 5, 6])
        self.assertIn("decision=answer_only", harness.system_prompt(state))

    def test_content_modules_keep_three_by_ten_pool_and_deliver_three_full_scripts(self):
        module_five = harness.MODULE_WORKFLOWS[5]["checkpoints"]
        module_six = harness.MODULE_WORKFLOWS[6]["checkpoints"]
        self.assertTrue(any("3 个" in item for item in module_five))
        self.assertTrue(any("每个种类" in item and "10 个" in item for item in module_five))
        self.assertTrue(any("3 篇精选" in item for item in module_six))
        self.assertTrue(any("完整口播文案" in item for item in module_six))

    def test_incomplete_legacy_state_cannot_skip_the_last_checkpoint(self):
        state = harness.normalize_state({
            "current_module": 1,
            "completed_modules": [],
            "module_step": 4,
            "intake": {"status": "complete", "round": 3, "answers": {}},
        })
        self.assertEqual(state["module_step"], 3)

    def test_confirmed_legacy_module_advances_without_parsing_model_words(self):
        state = harness.normalize_state({
            "current_module": 4,
            "completed_modules": [1, 2, 3, 4],
            "module_step": 5,
            "foundation_report": {"status": "confirmed"},
            "intake": {"status": "complete", "round": 3, "answers": {}},
        })
        self.assertEqual(state["current_module"], 5)
        self.assertEqual(state["module_step"], 0)

    def test_full_open_flow_advances_only_through_confirmations(self):
        state = self.complete_intake()
        for module in range(1, 5):
            self.assertEqual(state["current_module"], module)
            for _ in harness.MODULE_WORKFLOWS[module]["checkpoints"]:
                state, _ = self.confirm_checkpoint(state)
        self.assertEqual(state["foundation_report"]["status"], "generating")
        state["foundation_report"]["status"] = "confirmed"
        state["current_module"] = 5
        state["module_step"] = 0
        for module in (5, 6):
            for _ in harness.MODULE_WORKFLOWS[module]["checkpoints"]:
                state, _ = self.confirm_checkpoint(state)
        self.assertEqual(state["completed_modules"], [1, 2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
