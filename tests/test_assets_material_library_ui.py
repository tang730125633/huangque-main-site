import pathlib
import unittest


ASSETS_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/assets.html"


class AssetsMaterialLibraryUiTests(unittest.TestCase):
    """素材库页签（2026-09-23 素材库统一）。

    可出片的原料（本人上传的图片/音频/视频）与生成品（资产）分开管理：
    工作台这一页直接读 hq-ip-agent 的账号级素材接口（不带会话号），
    上传、删除、切片状态与云空间配额都在这页完成。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = ASSETS_HTML.read_text(encoding="utf-8")

    def test_material_tab_uses_account_scoped_ip12_endpoints(self):
        self.assertIn('data-cat="material"', self.html)
        self.assertIn("var MATERIAL_API='/workbench/ip12/api/v4'", self.html)
        self.assertIn("MATERIAL_API+'/assets?limit=200'", self.html)
        self.assertIn("MATERIAL_API+'/assets/preprocess'", self.html)
        self.assertIn("MATERIAL_API+'/assets/'+encodeURIComponent(item.id)", self.html)
        self.assertIn("method:'DELETE'", self.html)

    def test_material_upload_is_account_scoped_and_sends_no_session(self):
        upload_block = self.html.split("function uploadOneMaterial(", 1)[1].split(
            "function uploadMaterialFiles", 1
        )[0]
        self.assertIn("xhr.open('POST',MATERIAL_API+'/upload',true)", upload_block)
        self.assertIn("xhr.withCredentials=true", upload_block)
        self.assertNotIn("session_id", upload_block)
        self.assertIn("form.append('file',file)", upload_block)

    def test_material_tab_surfaces_slicing_status_and_quota(self):
        self.assertIn("已切成镜头", self.html)
        self.assertIn("正在切镜头…", self.html)
        self.assertIn("正在后台切镜头", self.html)
        self.assertIn("云空间 '+materialBytes(materialQuota.used)", self.html)
        self.assertIn("materialBytes(materialQuota.limit||2147483648)", self.html)

    def test_material_tab_renders_from_account_library_not_hosted_assets(self):
        body = self.html.split("if(cat==='material'){", 1)[1].split(
            "if(cat==='audio'){", 1
        )[0]
        self.assertIn("ensureMaterialLibrary()", body)
        self.assertIn("renderMaterialLibrary()", body)
        handler = self.html.split("document.querySelectorAll('#catRow .chip')", 1)[1].split(
            "function setCat", 1
        )[0]
        self.assertIn("if(cat==='material'){ render(); return; }", handler)
        self.assertIn("if(cat==='material'){ render(); } else { load(loadKindOf(cat)); }", self.html)

    def test_material_tab_guides_upload_when_empty(self):
        self.assertIn('id="materialUploadBtn"', self.html)
        self.assertIn('id="materialEmptyUpload"', self.html)
        self.assertIn('id="materialFile" type="file" accept="video/*,image/*,audio/*" multiple', self.html)
        self.assertIn("uploadMaterialFiles(fileInput.files)", self.html)
        self.assertIn("上传本人视频", self.html)

    def test_material_search_uses_page_search_box(self):
        self.assertIn("String(item.name||'').toLowerCase().indexOf(term)>=0", self.html)


if __name__ == "__main__":
    unittest.main()
