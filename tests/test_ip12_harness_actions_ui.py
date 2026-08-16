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

    def test_failed_turn_stays_visible_and_retries_without_duplicate_bubbles(self):
        cases = {
            "index.html": ("async function sendJumpMsg", "um", "am"),
            "index_clean.html": ("// ── Drawer", "userMsg", "aiMsg"),
        }
        for filename, (end_marker, user_node, assistant_node) in cases.items():
            with self.subTest(filename=filename):
                source = (TEMPLATES / filename).read_text(encoding="utf-8")
                start = source.index("async function sendTurn")
                send_turn = source[start:source.index(end_marker, start)]

                self.assertIn("回复暂时失败，消息已保留。", send_turn)
                self.assertIn('class="harness-action primary"', send_turn)
                self.assertNotIn("e.message", send_turn)
                self.assertIn(
                    f"{user_node}.remove();{assistant_node}.remove();sendTurn(turn,displayText,retryRequestId)",
                    send_turn,
                )
                self.assertIn("requestId||newTurnRequestId()", send_turn)
                self.assertIn("retryNeedsRefresh", send_turn)
                self.assertIn("status===409", send_turn)
                self.assertIn("failureMessage", send_turn)
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


if __name__ == "__main__":
    unittest.main()
