from __future__ import annotations

import importlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"


class MatrixTemplateSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(SERVER) not in sys.path:
            sys.path.insert(0, str(SERVER))
        cls.module = importlib.import_module(
            "content_domains.matrix_template_semantics"
        )

    def setUp(self):
        self.module._CACHE.clear()

    @staticmethod
    def contract():
        return {
            "version": 1,
            "max_width_px": 996,
            "layers": {
                "top1": {"font_size_px": 86, "max_lines": 2},
                "top2": {"font_size_px": 62, "max_lines": 4},
                "bottom2": {"font_size_px": 78, "max_lines": 2},
            },
        }

    def test_index_response_never_rewrites_source_and_snaps_numeric_break(self):
        top = "团队8个人，每天产出100条短视频，覆盖全部平台"
        bottom = "想进军健康赛道的，勾兑勾兑"
        commas = [index for index, char in enumerate(top) if char == "，"]
        response = {
            "choices": [{"message": {"content": json.dumps({
                "top1_end": 1,
                "top_break_after": [1, *commas],
                "bottom_break_after": [bottom.index("，")],
            })}}],
        }
        fake = mock.MagicMock()
        fake.__enter__.return_value = io.BytesIO(json.dumps(response).encode())
        fake.__exit__.return_value = False
        with mock.patch.object(self.module, "OPENAI_KEY", "configured"), \
             mock.patch.object(self.module.urllib.request, "urlopen", return_value=fake):
            value = self.module.generate(top, bottom, self.contract())
        self.assertEqual(commas[0], value["top1_end"])
        self.assertNotIn(1, value["top_break_after"])
        self.assertEqual(
            self.module._source_sha256(top, bottom), value["source_sha256"]
        )
        self.assertNotIn("top_text", value)
        self.assertNotIn("bottom_text", value)

    def test_initial_reuses_valid_cached_indices(self):
        top, bottom = "广州健康创业圈子", "评论区扣888"
        value = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": self.module._source_sha256(top, bottom),
            "top1_end": len(top) - 1,
            "top_break_after": [], "bottom_break_after": [],
        }
        with mock.patch.object(self.module, "generate", return_value=value) as generate:
            first_key, first = self.module.initial(
                top, bottom, "ref-02-fixture-02", self.contract()
            )
            second_key, second = self.module.initial(
                top, bottom, "ref-02-fixture-02", self.contract()
            )
        self.assertEqual((first_key, first), (second_key, second))
        generate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
