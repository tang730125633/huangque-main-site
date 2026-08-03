# -*- coding: utf-8 -*-

import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "site" / "workbench" / "one-click-video.html"


class OneClickVideoUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")

    def test_has_complete_review_and_render_flow(self):
        for marker in (
            "/api/gen/video/assets?limit=60",
            "/api/gen/video-compose/projects",
            "/analyze-source",
            "/edit-decisions",
            "/render",
            "开始分析",
            "确认粗剪",
            "渲染真实 MP4",
            "下载成片 MP4",
        ):
            self.assertIn(marker, self.html)

    def test_uses_asset_ids_and_never_accepts_arbitrary_source_urls(self):
        self.assertIn("source_asset_id:selected.id", self.html)
        self.assertNotIn("source_url:", self.html)
        self.assertNotIn("video_url:selected", self.html)

    def test_output_and_private_assets_are_fetched_with_authentication(self):
        self.assertIn("Authorization:'Bearer '+token", self.html)
        self.assertIn("protectedUrl(d.output_url)", self.html)

    def test_mobile_shell_can_scroll_to_review_and_render_panels(self):
        self.assertIn('@media(max-width:900px){.hq-app{overflow:auto!important}}', self.html)

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_inline_javascript_syntax(self):
        scripts = re.findall(r"<script(?: [^>]*)?>(.*?)</script>", self.html, re.S)
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "inline.js"
            target.write_text("\n".join(scripts), encoding="utf-8")
            subprocess.run(["node", "--check", str(target)], check=True,
                           capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
