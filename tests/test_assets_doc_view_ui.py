import pathlib
import unittest


ASSETS_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/assets.html"


class AssetsDocViewUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = ASSETS_HTML.read_text(encoding="utf-8")

    def test_copy_and_leads_have_actions_without_file_url(self):
        doc_card = self.html.split("function docCard(x){", 1)[1].split(
            "function renderDocAssets", 1
        )[0]
        self.assertIn("viewCopy.textContent='查看全文'", doc_card)
        self.assertIn("viewLeads.textContent='查看名单'", doc_card)
        self.assertLess(doc_card.index("if(kind==='copy')"), doc_card.index("if(x.url)"))
        self.assertLess(doc_card.index("if(kind==='leads')"), doc_card.index("if(x.url)"))

    def test_historical_assets_load_owned_job_result(self):
        loader = self.html.split("function loadDocPayload(x){", 1)[1].split(
            "function runDocAction", 1
        )[0]
        self.assertIn("'/api/gen/job/'+encodeURIComponent(x.job_id)", loader)
        self.assertIn("credentials:'same-origin'", loader)
        self.assertIn("mergeDocPayload(x,d.result||{})", loader)

    def test_copy_body_supports_text_and_scenes(self):
        formatter = self.html.split("function copyBody(data){", 1)[1].split(
            "function safeDocUrl", 1
        )[0]
        self.assertIn("data.body||data.text", formatter)
        self.assertIn("Array.isArray(data.scenes)", formatter)
        self.assertIn("'镜号'+('0'+(i+1)).slice(-2)", formatter)

    def test_leads_modal_uses_safe_dom_fields_and_http_links(self):
        modal = self.html.split("function openLeadsModal(payload,x){", 1)[1].split(
            "function openLeadsAsset", 1
        )[0]
        self.assertIn("name.textContent=item.nickname||'匿名用户'", modal)
        self.assertIn("content.textContent=item.content||'（无需求原文）'", modal)
        self.assertIn("sourceText.textContent='来源：'", modal)
        self.assertIn("safeDocUrl(pair[1])", modal)
        self.assertNotIn("innerHTML", modal)


if __name__ == "__main__":
    unittest.main()
