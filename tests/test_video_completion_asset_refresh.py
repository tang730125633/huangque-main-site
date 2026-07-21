from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
VIDEO_HTML = ROOT / "site" / "workbench" / "video.html"


class VideoCompletionAssetRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = VIDEO_HTML.read_text(encoding="utf-8")

    def test_completed_job_retries_until_downloadable_asset_is_persisted(self):
        self.assertIn("function refreshCompletedVideoAsset(jobId,attempt)", self.source)
        self.assertIn("if(target && target.video_url)", self.source)
        self.assertIn("refreshCompletedVideoAsset(id,0);", self.source)

    def test_completion_no_longer_relies_on_fixed_history_delay(self):
        self.assertNotIn("setTimeout(loadVideoHistory,800)", self.source)
        self.assertIn("保留 jobs 返回的临时卡片", self.source)


if __name__ == "__main__":
    unittest.main()
