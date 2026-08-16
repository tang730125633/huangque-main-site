import pathlib
import unittest


COLLECT_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/collect.html"


class CollectPollFailedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = COLLECT_HTML.read_text(encoding="utf-8")

    def test_main_and_transcript_pollers_handle_failed_terminal_status(self):
        terminal_check = "d.status === 'error' || d.status === 'failed'"
        self.assertEqual(self.html.count(terminal_check), 2)

    def test_frontend_giveup_remains_below_backend_collect_grace(self):
        self.assertIn("var POLL_GIVEUP_SEC = 600", self.html)
        self.assertIn("core.py::reaper 给 collect 的 1200s 窗口", self.html)

    def test_successful_download_replaces_stale_error_note(self):
        self.assertIn("setNote('✅ 下载已开始', '#2bd576')", self.html)


if __name__ == "__main__":
    unittest.main()
