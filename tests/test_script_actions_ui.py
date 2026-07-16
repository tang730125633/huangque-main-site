import pathlib
import unittest


SCRIPT_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/script.html"


class ScriptActionsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = SCRIPT_HTML.read_text(encoding="utf-8")

    def test_scene_handoffs_keep_prompt_parameters(self):
        self.assertIn("handoffUrl('video.html',a.getAttribute('data-to-video')", self.html)
        self.assertIn("handoffUrl('audio.html',b.getAttribute('data-to-audio')", self.html)
        self.assertIn("'?prompt='+encodeURIComponent(prompt||'')", self.html)
        self.assertIn("escAttr(s.scene||'')", self.html)
        self.assertIn("escAttr(s.line||'')", self.html)

    def test_export_builds_utf8_text_download(self):
        self.assertIn('id="scExport"', self.html)
        self.assertIn("new Blob(['\\ufeff'+scriptText(exportScenes)]", self.html)
        self.assertIn("a.download=filename", self.html)

    def test_one_click_video_calls_script_to_video_api(self):
        self.assertIn('id="scOneClickVideo"', self.html)
        self.assertIn("fetch('/api/gen/script_to_video'", self.html)
        self.assertIn("scenes:scenesForHandoff", self.html)
        self.assertIn("_resetOneClick", self.html)

    def test_history_loads_copy_assets_and_restores_scenes(self):
        self.assertIn("'/api/gen/assets?limit=60&kind=copy'", self.html)
        self.assertIn("historyList.appendChild(historyCard(item))", self.html)
        self.assertIn("render({scenes:list},heading", self.html)

    def test_history_controls_are_accessible_buttons(self):
        self.assertIn('id="scHistoryBtn" class="sc-btn" type="button"', self.html)
        self.assertIn("btn.type='button'; btn.className='sc-history-item'", self.html)


if __name__ == "__main__":
    unittest.main()
