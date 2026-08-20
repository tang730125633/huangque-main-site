import unittest
from pathlib import Path


TEMPLATES = Path(__file__).parents[1] / "server" / "hermes_ip12" / "templates"


class IP12HarnessActionsUITests(unittest.TestCase):
    def test_only_latest_assistant_reply_keeps_confirmation_actions(self):
        # Regression: ISSUE-001 — old assistant replies repeated the current action buttons.
        # Found by /qa on 2026-08-12.
        # Report: local visible QA for PR 1056.
        for filename in ("index.html", "index_clean.html"):
            with self.subTest(filename=filename):
                source = (TEMPLATES / filename).read_text(encoding="utf-8")
                attach = source[source.index("function attachHarnessActions"):source.index("function renderChat")]
                self.assertIn("document.querySelectorAll('#chatArea .harness-actions')", attach)

    def test_main_view_recovers_an_interrupted_reply_from_the_existing_receipt(self):
        source = (TEMPLATES / "index.html").read_text(encoding="utf-8")
        recovery = source[source.index("async function recoverTurn"):source.index("async function sendTurn")]
        send_turn = source[source.index("async function sendTurn"):source.index("async function sendJumpMsg")]

        self.assertIn("?receipt=", recovery)
        self.assertIn("连接中断，正在自动找回这条回复", recovery)
        self.assertIn("r.status===202", recovery)
        self.assertIn("80", recovery)
        self.assertIn("await recoverTurn(turnCid,retryRequestId,bubble)", send_turn)
        self.assertIn("await selectConvo(turnCid)", send_turn)
        self.assertIn("重新载入 Project", send_turn)
        self.assertNotIn("回复暂时失败，消息已保留。", send_turn)
        self.assertNotIn("sendTurn(turn,displayText,retryRequestId)", send_turn)
        self.assertIn("data.replayed", send_turn)
        self.assertIn("data.actions", send_turn)
        self.assertRegex(send_turn, r"data\.state\.revision>=\w+\.revision")

    def test_foundation_report_gate_keeps_chat_actionable(self):
        for filename in ("index.html", "index_clean.html"):
            with self.subTest(filename=filename):
                source = (TEMPLATES / filename).read_text(encoding="utf-8")
                state_actions = source[source.index("function stateActions"):source.index("function renderChat")]
                self.assertIn("foundation.status==='awaiting_confirmation'", state_actions)
                self.assertIn("open_foundation_report", state_actions)
                self.assertIn("confirm_foundation_report", state_actions)
                self.assertIn("edit_foundation_report", state_actions)
                self.assertIn("regenerate_foundation_report", state_actions)
                self.assertIn("review_status==='dirty'", state_actions)
                self.assertIn("查看 PDF", state_actions)
                self.assertIn("需要修改/补充", state_actions)
                self.assertIn("重新生成 PDF", state_actions)
                self.assertIn("确认初稿，进入模块 5", state_actions)
                self.assertIn("runStateAction(item)", state_actions)

                send_message = source[source.index("function sendMessage"):source.index("async function sendTurn")]
                self.assertIn("foundation_review", send_message)
                self.assertIn("foundationEditing", send_message)

    def test_content_pack_targets_one_script_from_both_views(self):
        for filename in ("index.html", "index_clean.html"):
            with self.subTest(filename=filename):
                source = (TEMPLATES / filename).read_text(encoding="utf-8")
                self.assertIn("content_pack_v1", source)
                self.assertIn("content_target", source)
                self.assertIn("category_id", source)
                self.assertIn("topic_id", source)
                self.assertIn("versions", source)

    def test_main_view_opens_new_module_six_delivery_in_the_right_panel(self):
        source = (TEMPLATES / "index.html").read_text(encoding="utf-8")
        send_turn = source[
            source.index("async function sendTurn"):
            source.index("async function sendJumpMsg")
        ]

        self.assertIn("if(data.auto_deliverables['6'])", send_turn)
        self.assertIn("openPanel('📦 文案口播交付物')", send_turn)
        self.assertIn("renderContentPack(document.getElementById('rpnBody'))", send_turn)

    def test_production_panel_only_asks_for_missing_user_inputs(self):
        source = (TEMPLATES / "index.html").read_text(encoding="utf-8")
        field_specs = source[
            source.index("function productionFieldSpecs"):
            source.index("function coerceProductionOption")
        ]
        field_spec = source[
            source.index("function productionFieldSpec"):
            source.index("function productionOptionFilled")
        ]
        options_html = source[
            source.index("function productionOptionsHtml"):
            source.index("function productionQuote")
        ]
        field_control = source[
            source.index("function productionFieldControl"):
            source.index("function productionOptionsHtml")
        ]

        self.assertIn("if(!missing.length)required.forEach", field_specs)
        self.assertIn("descriptor.oneOf", field_spec)
        self.assertIn("choice.const", field_spec)
        self.assertIn("!fields.some", options_html)
        self.assertIn("productionDisplayValue(record,key,options[key])", options_html)
        self.assertIn("if(canvas){", source)
        self.assertNotIn("if(canvas||kind==='canvas')", source)
        self.assertIn("暂无可用选项", field_control)
        self.assertIn("choice.value", field_control)
        self.assertIn("ratio:'画面比例'", source)
        self.assertIn("avatar_id:'数字人形象'", source)
        self.assertIn("board_id:'画布'", source)

    def test_main_view_keeps_artifacts_in_module_dropdowns_without_auto_opening(self):
        source = (TEMPLATES / "index.html").read_text(encoding="utf-8")
        select_convo = source[
            source.index("async function selectConvo"):
            source.index("async function jumpModule")
        ]
        send_turn = source[
            source.index("async function sendTurn"):
            source.index("async function sendJumpMsg")
        ]

        self.assertIn("function moduleArtifactRows", source)
        self.assertIn('class="mod-sub artifact"', source)
        self.assertIn("data-module-id", source)
        self.assertNotIn('id="artifactsWrap"', source)
        self.assertNotIn("openFirstContentScript", select_convo)
        self.assertNotIn("openFirstContentScript", send_turn)
        self.assertIn("showArtifactNotice(data)", send_turn)
        self.assertIn("第一个交付物已放到对应诊断模块下方", source)

    def test_main_view_exposes_a_two_project_manager(self):
        source = (TEMPLATES / "index.html").read_text(encoding="utf-8")
        start = source.index("function renderProjectPanel")
        manager = source[start:source.index("document.getElementById('userInput').addEventListener", start)]
        self.assertIn("IP Project", manager)
        self.assertIn("/2", manager)
        self.assertIn("最多允许创建两个 Project", source)
        self.assertIn("永久删除", source)
        self.assertIn("o.style.display='flex'", manager)
        self.assertLess(manager.index("o.style.display='flex'"), manager.index("await loadConvos()"))


if __name__ == "__main__":
    unittest.main()
