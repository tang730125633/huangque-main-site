# -*- coding: utf-8 -*-
import re
import unittest
from pathlib import Path


BANANA = (Path(__file__).resolve().parents[1] / "site" / "workbench" / "banana.html").read_text(encoding="utf-8")


class HistoryReferenceTests(unittest.TestCase):
    def test_shared_loader_converts_remote_image_into_reference(self):
        match = re.search(
            r"function loadReferenceFromUrl\(url\)\{(?P<body>.*?)\n  \}",
            BANANA,
            re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertIn("referenceFetchUrl(url)", body)
        self.assertIn("r.blob()", body)
        self.assertIn("refFromFile(blob", body)
        self.assertIn("openInp()", body)

    def test_remote_results_use_authenticated_same_origin_proxy(self):
        self.assertIn("'/api/gen/dl?url='+encodeURIComponent(parsed.href)", BANANA)
        self.assertIn("credentials:'same-origin'", BANANA)
        self.assertNotIn("im.crossOrigin='anonymous';\n    im.onerror=function(){ setNote('继续改", BANANA)

    def test_history_action_closes_modal_then_loads_selected_reference(self):
        self.assertRegex(
            BANANA,
            r"use\.onclick=function\(\)\{\s*var url=urls\[idx\];\s*closeModal\(\);\s*loadReferenceFromUrl\(url\);\s*\};",
        )

    def test_continue_edit_reuses_shared_reference_loader(self):
        block = BANANA[BANANA.index("if(bEdit) bEdit.onclick=function()") :]
        block = block[: block.index("if(bVideo)")]
        self.assertIn("loadReferenceFromUrl(lastResultUrl)", block)
        self.assertNotIn("new Image()", block)


if __name__ == "__main__":
    unittest.main()
