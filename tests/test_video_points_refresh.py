import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")


def block(start, end):
    value = HTML[HTML.index(start):]
    return value[:value.index(end)]


class VideoPointsRefreshTests(unittest.TestCase):
    def test_refresh_helper_uses_shared_shell_balance(self):
        helper = block("function refreshVideoPoints(){", "function pollJob")
        self.assertIn("window.HQ&&window.HQ.refreshPoints", helper)
        self.assertIn("window.HQ.refreshPoints()", helper)

    def test_each_video_submission_refreshes_after_deduction(self):
        submissions = (
            ("function submitTryon(){", "function talkingPayload"),
            ("function submitVideo(){", "function submitXiaole"),
            ("function submitXiaole(channel){", "$('generateBtn')"),
        )
        for start, end in submissions:
            with self.subTest(start=start):
                value = block(start, end)
                self.assertIn("if(!res.data.job_id)", value)
                self.assertIn("refreshVideoPoints();", value)
                self.assertLess(value.index("if(!res.data.job_id)"), value.index("refreshVideoPoints();"))

    def test_terminal_job_state_reconciles_balance(self):
        value = block("function pollJob(id, tries){", "function submitTryon")
        done = value[value.index("if(d.status==='done'"):value.index("if(d.status==='error'")]
        self.assertIn("refreshVideoPoints();", done)
        self.assertIn("if(e&&e.jobTerminal){refreshVideoPoints();", value)


if __name__ == "__main__":
    unittest.main()
