import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = (ROOT / "site/workbench/assets.html").read_text(encoding="utf-8")


class AssetsVideoCoverUiTests(unittest.TestCase):
    def test_missing_cover_uses_lazy_first_video_frame(self):
        self.assertIn("function queueVideoFrame(video,url)", ASSETS)
        self.assertIn("new IntersectionObserver", ASSETS)
        self.assertIn("{rootMargin:'240px 0px'}", ASSETS)
        self.assertIn("preview+='#t=0.1'", ASSETS)
        self.assertIn("else queueVideoFrame(v,videoUrl)", ASSETS)

    def test_existing_cover_stays_preferred(self):
        self.assertIn("if(imageUrl) v.poster=thumbUrl(imageUrl,420)", ASSETS)

    def test_rerender_disconnects_stale_video_observers(self):
        body = ASSETS[ASSETS.index("function _renderBody(){"):]
        self.assertLess(
            body.index("resetVideoFrameObserver()"),
            body.index("visibleItems=[]"),
        )


if __name__ == "__main__":
    unittest.main()
