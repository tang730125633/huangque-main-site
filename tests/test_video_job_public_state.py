import pathlib
import sqlite3
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import core


class VideoJobPublicStateTests(unittest.TestCase):
    def _row(self, status, refunded=0):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT 1 id, 'video' kind, 'fang' username, 20 cost, ? status, "
            "NULL result, 'upstream failed' error, 1 created_at, 2 updated_at, ? refunded",
            (status, refunded),
        ).fetchone()
        conn.close()
        return row

    def test_failed_job_overrides_stale_downloading_phase_and_exposes_refund(self):
        public = core._job_public_dict(self._row("failed", refunded=1), "downloading")
        self.assertEqual("failed", public["phase"])
        self.assertIs(True, public["refunded"])

    def test_done_job_has_done_phase(self):
        public = core._job_public_dict(self._row("done"), "downloading")
        self.assertEqual("done", public["phase"])
        self.assertIs(False, public["refunded"])

    def test_pending_refund_is_not_reported_as_confirmed(self):
        public = core._job_public_dict(self._row("error", refunded=2))
        self.assertIs(False, public["refunded"])

    def test_script_to_video_reads_real_render_phase(self):
        video_domain = mock.Mock()
        video_domain.get_video_job_phase.return_value = "talking_render"
        with mock.patch.object(core, "_domains", return_value=(mock.Mock(), mock.Mock(), video_domain)):
            self.assertEqual("talking_render", core._video_job_phase_for_public(91, "script_to_video"))
        video_domain.get_video_job_phase.assert_called_once_with(91)

    def test_non_video_job_does_not_query_video_phase(self):
        video_domain = mock.Mock()
        with mock.patch.object(core, "_domains", return_value=(mock.Mock(), mock.Mock(), video_domain)):
            self.assertIsNone(core._video_job_phase_for_public(92, "audio"))
        video_domain.get_video_job_phase.assert_not_called()


if __name__ == "__main__":
    unittest.main()
