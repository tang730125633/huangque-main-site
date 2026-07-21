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

    def test_history_loads_copy_assets_and_restores_scenes(self):
        self.assertIn("'/api/gen/assets?limit=60&kind=copy'", self.html)
        self.assertIn("historyList.appendChild(historyCard(item))", self.html)
        self.assertIn("render({scenes:list,version:m.version||1,industry:m.industry},heading", self.html)

    def test_history_controls_are_accessible_buttons(self):
        self.assertIn('id="scHistoryBtn" class="sc-btn" type="button"', self.html)
        self.assertIn("btn.type='button'; btn.className='sc-history-item'", self.html)

    def test_empty_state_replaces_demo_storyboard_and_disables_actions(self):
        self.assertIn('id="scEmptyState"', self.html)
        self.assertIn("setActionsEnabled(false)", self.html)

    def test_industry_edit_save_and_regenerate_controls_exist(self):
        self.assertIn('id="segIndustry"', self.html)
        self.assertIn('id="scSave"', self.html)
        self.assertIn('id="scRegenerate"', self.html)
        self.assertIn("fetch('/api/gen/copy/'+currentJobId", self.html)
        self.assertIn("payload.parent_job_id=regenerateParent", self.html)

    def test_scenes_render_as_editable_fields(self):
        self.assertIn('data-scene-field="scene"', self.html)
        self.assertIn('data-scene-field="line"', self.html)
        self.assertIn('data-scene-field="dur"', self.html)


if __name__ == "__main__":
    unittest.main()
