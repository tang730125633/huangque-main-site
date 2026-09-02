import pathlib
import json
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = (ROOT / "site/workbench/assets.html").read_text(encoding="utf-8")


class AssetsVideoCoverUiTests(unittest.TestCase):
    @classmethod
    def runtime(cls):
        result = subprocess.run(
            ["node", str(ROOT / "tests/assets_video_cover_runtime.cjs")],
            check=True, capture_output=True, text=True,
        )
        return json.loads(result.stdout)

    def test_missing_cover_uses_lazy_first_video_frame(self):
        self.assertIn("function queueVideoFrame(video,url)", ASSETS)
        self.assertIn("new IntersectionObserver", ASSETS)
        self.assertIn("{rootMargin:'240px 0px'}", ASSETS)
        self.assertIn("preview+='#t=0.1'", ASSETS)
        self.assertIn("else queueVideoFrame(v,videoUrl)", ASSETS)
        self.assertNotIn("video.preload='auto'", ASSETS)

    def test_existing_cover_stays_preferred(self):
        self.assertIn("if(imageUrl) v.poster=thumbUrl(imageUrl,420)", ASSETS)

    def test_rerender_disconnects_stale_video_observers(self):
        body = ASSETS[ASSETS.index("function _renderBody(){"):]
        self.assertLess(
            body.index("resetVideoFrameObserver()"),
            body.index("visibleItems=[]"),
        )

    def test_runtime_loads_local_and_remote_first_frames(self):
        result = self.runtime()
        for key in ("local", "remote"):
            item = result[key]
            self.assertTrue(item["src"])
            self.assertEqual("metadata", item["preload"])
            self.assertEqual("", item["dataSrc"])
            self.assertEqual(1, item["loads"])
            self.assertEqual(0.1, item["currentTime"])

    def test_runtime_rerender_stops_activated_requests(self):
        result = self.runtime()
        for key in ("localCleanup", "remoteCleanup"):
            item = result[key]
            self.assertEqual("", item["src"])
            self.assertEqual(1, item["pauses"])
            self.assertEqual(2, item["loads"])
        self.assertTrue(result["observerDisconnected"])


if __name__ == "__main__":
    unittest.main()
