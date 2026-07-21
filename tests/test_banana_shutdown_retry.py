# -*- coding: utf-8 -*-
import re
import unittest
from pathlib import Path


BANANA = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "banana.html").read_text(encoding="utf-8")


class ShutdownRetryTests(unittest.TestCase):
    def test_only_explicit_pre_deduction_shutdown_response_is_retried(self):
        self.assertIn("var MAX_SHUTDOWN_RETRIES=3", BANANA)
        self.assertRegex(
            BANANA,
            r"x\.s===503\s*&&\s*x\.d\s*&&\s*x\.d\.code==='shutting_down'",
        )
        self.assertNotRegex(BANANA, r"x\.s>=500[^\n]+sendSubmit")

    def test_retry_keeps_submission_locked_and_uses_server_delay(self):
        submit = BANANA[BANANA.index("function submit(payload, label, endpoint)") :]
        submit = submit[: submit.index("// ===== 参考图上传")]
        retry = submit.index("x.d.code==='shutting_down'")
        unlock = submit.index("submitting=false", retry)
        resubmit = submit.index("sendSubmit(attempt+1)", retry)
        self.assertLess(resubmit, unlock)
        self.assertIn("x.d.retry_after_ms", submit[retry:resubmit])
        self.assertIn("正在自动重试", submit[retry:resubmit])

    def test_retry_budget_stops_after_three_attempts(self):
        self.assertRegex(
            BANANA,
            r"attempt<MAX_SHUTDOWN_RETRIES[\s\S]+sendSubmit\(attempt\+1\)",
        )
        self.assertIn("sendSubmit(0)", BANANA)


if __name__ == "__main__":
    unittest.main()
