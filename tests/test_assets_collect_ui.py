import pathlib
import unittest


ASSETS_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/assets.html"


class AssetsCollectUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = ASSETS_HTML.read_text(encoding="utf-8")

    def test_collect_preview_uses_authenticated_proxy_for_temporary_links(self):
        self.assertIn("'/api/gen/dl?url='+encodeURIComponent(url)", self.html)
        self.assertIn("credentials:'same-origin'", self.html)
        self.assertIn("proxy:meta.permanent===false", self.html)
        self.assertIn("openAssetVideoModal(x.url,x.title,meta.cover", self.html)

    def test_expired_collect_link_has_actionable_error(self):
        self.assertIn("视频链接已过期，请重新采集后再试", self.html)

    def test_collect_card_no_longer_opens_video_url_in_a_new_tab(self):
        doc_card = self.html.split("function docCard(x){", 1)[1].split(
            "function renderDocAssets", 1
        )[0]
        collect_block = doc_card.split("if(kind==='collect'){", 1)[1].split("}else{", 1)[0]
        self.assertIn("document.createElement('button')", collect_block)
        self.assertNotIn("target='_blank'", collect_block)

    def test_asset_page_has_visible_scrollbar(self):
        self.assertIn("document.addEventListener('DOMContentLoaded',markAssetPageScroll)", self.html)
        self.assertIn("assetPageScroll.classList.add('asset-page-scroll')", self.html)
        self.assertIn(".asset-page-scroll::-webkit-scrollbar-thumb", self.html)
        self.assertIn("scrollbar-color:rgba(231,178,76,.62)", self.html)


if __name__ == "__main__":
    unittest.main()
