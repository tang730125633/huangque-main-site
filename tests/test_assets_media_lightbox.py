import pathlib
import unittest


ASSETS_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/assets.html"


class AssetsMediaLightboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = ASSETS_HTML.read_text(encoding="utf-8")

    def test_lightbox_exists_and_scoped_to_grid(self):
        """资产卡片图片/视频可点击放大（灯箱，限定 assetGrid 容器）"""
        self.assertIn("function _openMediaLightbox(kind, src)", self.html)
        self.assertIn("hqMediaLightbox", self.html)
        self.assertIn("t.closest('#assetGrid')", self.html)

    def test_multi_select_mode_skips_lightbox(self):
        """多选模式下点击不触发灯箱"""
        self.assertIn("if(multi) return;", self.html)


if __name__ == "__main__":
    unittest.main()
