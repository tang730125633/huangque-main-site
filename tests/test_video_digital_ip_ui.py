import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")


class DigitalIpUiTests(unittest.TestCase):
    def setUp(self):
        self.panel = HTML.split('id="talkingPanel"', 1)[1].split('id="cinematicPanel"', 1)[0]

    def test_three_step_workflow_is_visible(self):
        self.assertIn('aria-label="数字化 IP 生成流程"', self.panel)
        for number, label in (("01", "形象与内容"), ("03", "画面与字幕")):
            self.assertIn("<span>{}</span><b>{}</b>".format(number, label), self.panel)
        self.assertIn('<span>02</span><b id="talkingFlowSound">选择音色</b>', self.panel)
        self.assertIn("准备口播音频", self.panel)

    def test_primary_sections_keep_existing_controls(self):
        for control_id in ("imageDrop", "scriptText", "voicePanel", "talkingParameterPanel", "bgmPanel"):
            self.assertIn('id="{}"'.format(control_id), self.panel)

    def test_background_music_follows_required_settings(self):
        self.assertLess(self.panel.index('id="talkingParameterPanel"'), self.panel.index('id="bgmPanel"'))

    def test_submit_bar_groups_cost_status_and_action(self):
        submit = self.panel.split('<div class="talking-submit">', 1)[1]
        for control_id in ("talkingCostSummary", "statusText", "generateBtn"):
            self.assertIn('id="{}"'.format(control_id), submit)


if __name__ == "__main__":
    unittest.main()
