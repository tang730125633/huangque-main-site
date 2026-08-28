import hashlib
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


os.environ.setdefault("OPENAI_API_KEY", "test-key")
HERMES = Path(__file__).parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import ip12_harness as harness
import server


class IP12PersonaReportV1Tests(unittest.TestCase):
    def test_report_input_preserves_candidates_and_selection(self):
        state = harness.initial_state()
        choices = [
            {"choice_id": f"choice-{index}", "display_index": index, "title": f"方向{index}",
             "summary": "摘要", "reason": "适合原因", "caution": "注意事项", "recommended": index == 2}
            for index in (1, 2, 3)
        ]
        state["ip_profile"]["confirmed_outputs"]["1-2"] = {
            "title": "定位选择", "content": "方向2",
            "choice_snapshot": {"choices": choices, "selected_choice_id": "choice-2"},
        }
        output = server._foundation_confirmed_outputs(state)["1-2"]
        self.assertEqual(len(output["candidates"]), 3)
        self.assertEqual(output["selected_choice_id"], "choice-2")
        self.assertTrue(output["candidates"][1]["recommended"])
        self.assertNotIn("choice_snapshot", harness.profile_for_model(state)["confirmed_outputs"]["1-2"])

    def test_foundation_artifact_records_and_checks_file_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project-1.pdf"
            path.write_bytes(b"%PDF-test-content%%EOF\n")
            with mock.patch.object(server, "_foundation_pdf_page_count", return_value=8):
                artifact = server._foundation_artifact(path, "report-1", 2)
                report = {"artifact": artifact}
                self.assertEqual(artifact["version"], 2)
                self.assertEqual(artifact["page_count"], 8)
                self.assertEqual(artifact["sha256"], "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(server._validate_foundation_artifact(report, path), 8)
                path.write_bytes(b"%PDF-changed%%EOF\n")
                with self.assertRaisesRegex(RuntimeError, "metadata"):
                    server._validate_foundation_artifact(report, path)

    def test_foundation_pdf_accepts_concise_six_page_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project-1.pdf"
            path.write_bytes(b"%PDF-test-content%%EOF\n")
            with mock.patch.object(server, "_foundation_pdf_page_count", return_value=6):
                self.assertEqual(server._validate_foundation_pdf(path), 6)

    def test_internal_intake_coverage_keeps_private_fields_out_of_customer_pdf(self):
        state = harness.initial_state()
        state["intake"]["declined_fields"] = list(harness.INTAKE_COVERAGE_FIELDS)
        for field, value in (("preferred_name", "阿青"), ("mobile", "13000000000")):
            state["intake"]["declined_fields"].remove(field)
            state["ip_profile"]["facts"][field] = {
                "field": field, "value": value, "kind": "user_fact", "evidence_quote": value,
            }
        coverage = server._foundation_intake_coverage(state)
        self.assertEqual(len(coverage), len(harness.INTAKE_COVERAGE_FIELDS))
        mobile = next(item for item in coverage if item["field"] == "mobile")
        self.assertEqual(mobile["value"], "已提供（隐私信息不在报告展示）")
        source = inspect.getsource(server.generate_foundation_report)
        self.assertNotIn("_foundation_intake_summary_markdown", source)
        self.assertIn('{"age", "gender", "mobile", "income_source", "income_range"}', source)

    def test_customer_pdf_hides_internal_labels_and_waits_for_final_title(self):
        content = (
            "### 故事传播卡（AI包装建议）\n建议\n\n"
            "### 待本人确认项\n- 有效咨询口径\n"
        )
        self.assertIn("故事表达建议", server._customer_facing_foundation_content(content))
        self.assertNotIn("AI包装建议", server._customer_facing_foundation_content(content))
        self.assertEqual(
            server._customer_facing_foundation_content("重复的完整说明文本必须只出现一次。\n重复的完整说明文本必须只出现一次。"),
            "重复的完整说明文本必须只出现一次。",
        )
        self.assertEqual(
            server._customer_facing_foundation_content("事实原话：这是需要保留的完整事实。\n- 事实原话：这是需要保留的完整事实。"),
            "事实原话：这是需要保留的完整事实。",
        )
        customer = server._customer_facing_foundation_content(
            "用户原话：测试。\n属于AI包装建议。"
        )
        self.assertIn("本人原话", customer)
        self.assertIn("传播包装建议", customer)
        self.assertNotIn("用户原话", customer)
        self.assertNotIn("AI包装建议", customer)
        self.assertEqual(server._foundation_report_title(content), "IP 人设定位｜模块 1-4 初稿")
        self.assertEqual(server._foundation_report_title("### 待本人确认项\n无待补充项"), "IP 人设定位报告｜模块 1-4")

    def test_customer_pdf_uses_current_story_facts_and_marks_incomplete_traits(self):
        state = harness.initial_state()
        state["ip_profile"]["facts"].update({
            "story_pitfall": {"value": "没算成本就签长租", "evidence_quote": "没算成本就签长租"},
            "biggest_setback": {"value": "租棚后现金流紧张", "evidence_quote": "租棚后现金流紧张"},
            "story_comeback": {"value": "退掉大棚并转为上门拍摄", "evidence_quote": "退掉大棚并转为上门拍摄"},
            "personality_traits": {"value": "真诚,克制", "evidence_quote": "真诚克制"},
        })
        content = (
            "表达边界：只陈述经营中因盲目扩张走过一段弯路及已确认调整，不延伸为店铺失败、关闭或倒闭。\n\n"
            "### 待本人确认项\n\n- 其他项"
        )
        result = server._foundation_apply_current_state(content, state)
        self.assertNotIn("盲目扩张", result)
        self.assertIn("没算成本就签长租", result)
        self.assertIn("退掉大棚并转为上门拍摄", result)
        self.assertIn("第三个性格词（当前仅确认：真诚、克制）", result)

    def test_report_prompt_requires_candidates_and_selected_choice(self):
        source = inspect.getsource(server.generate_foundation_report)
        self.assertIn("候选保留要求", source)
        self.assertIn("selected_choice_id", source)
        self.assertIn("reusable_content or call_ai", source)
        self.assertIn("传播表达建议（AI包装建议）", source)
        self.assertIn("执行优先级（AI包装建议）", source)
        self.assertIn("template_version", source)
        self.assertIn("首页｜IP结论总览", server.EDITORIAL_REPORT_PROMPT)
        self.assertIn("事实附录与确认清单", server.EDITORIAL_REPORT_PROMPT)
        self.assertIn("EDITORIAL_REPORT_PROMPT", source)
        self.assertIn("定位层级合同", server.EDITORIAL_REPORT_PROMPT)
        self.assertIn("有效咨询口径", server.EDITORIAL_REPORT_PROMPT)
        self.assertIn("P0｜起步", server.EDITORIAL_REPORT_PROMPT)
        self.assertIn(
            'report.get("status") not in {"awaiting_confirmation", "confirmed"}',
            inspect.getsource(server._process_foundation_revision_turn),
        )
        self.assertIn(
            "FOUNDATION_REPORT_TEMPLATE_VERSION",
            inspect.getsource(server.api_generate_foundation_report),
        )
        self.assertIn(
            "intake_incomplete_fields",
            inspect.getsource(server.api_confirm_foundation_report),
        )

    def test_module_six_action_turn_no_longer_auto_prepares_production(self):
        source = inspect.getsource(server._process_action_turn)
        self.assertIn('attempts = 2 if next_state.get("module_step") == 0 else 1', source)
        self.assertNotIn("_post_module_six_production_action", source)
        self.assertNotIn(
            "_post_module_six_production_action",
            inspect.getsource(server.api_get_convo),
        )

    def test_grounded_story_section_keeps_labeled_packaging_advice(self):
        content = (
            "## 模块四｜故事资产挖掘\n旧故事\n\n"
            "### 故事传播卡（AI包装建议）\n#### 复盘故事卡\n情绪曲线：建议。\n\n"
            "## 优化建议汇总\nP0 建议。"
        )
        result = server._ground_foundation_story_section(
            content, {"4-4": {"content": "事实原话：我开过一家小店。"}}
        )
        self.assertIn("事实原话：我开过一家小店。", result)
        self.assertIn("故事传播卡（AI包装建议）", result)
        self.assertIn("复盘故事卡", result)

    def test_foundation_story_sections_remove_contained_duplicates(self):
        content = (
            "### 故事资产清单\n\n"
            "#### 1. 完整翻身\n事实原话：退掉大棚改上门；最大坑是没算成本就签长租。\n\n"
            "#### 2. 重复踩坑\n事实原话：最大坑是没算成本就签长租。\n\n"
            "#### 3. 公益项目\n事实原话：带过3人小组做公益领养项目\n"
        )
        result = server._dedupe_contained_story_sections(content)
        self.assertNotIn("重复踩坑", result)
        self.assertIn("#### 2. 公益项目", result)

    def test_reviewed_story_correction_is_not_overwritten_by_old_snapshot(self):
        content = "## 模块四｜故事资产挖掘\n\n### 已确认故事资产\n\n经营中走过一段弯路。"
        result = server._ground_foundation_story_section(
            content, {"4-4": {"content": "旧版故事快照"}}, [{"content": "修正措辞"}]
        )
        self.assertEqual(result, content)

    def test_weather_shortcut_does_not_hijack_non_weather_temperature_wording(self):
        self.assertIsNone(server._WEATHER_RE.search("记录社区里那些很普通但有温度的送花故事"))
        self.assertIsNotNone(server._WEATHER_RE.search("今天气温多少度"))


if __name__ == "__main__":
    unittest.main()
