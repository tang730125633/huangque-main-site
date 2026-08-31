from __future__ import annotations

import importlib
import concurrent.futures
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

    @staticmethod
    def v05_contract():
        return {
            "version": 1,
            "max_width_px": 996,
            "layers": {
                "top1": {"font_size_px": 102, "max_lines": 2},
                "top2": {"font_size_px": 104, "max_lines": 2},
                "top3": {"font_size_px": 68, "max_lines": 2},
                "bottom2": {"font_size_px": 70, "max_lines": 2},
            },
        }

    def test_prompt_exposes_v05_top3_to_semantic_model(self):
        prompt = self.module._prompt(
            "团队8个人，每天产出100条短视频",
            "评论区扣111",
            self.v05_contract(),
        )
        self.assertIn("top3: 68px", prompt)
        self.assertIn("分配到 top3", prompt)

    def test_index_response_never_rewrites_source(self):
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
        self.assertEqual(1, value["top1_end"])
        self.assertIn(1, value["top_break_after"])
        self.assertEqual(
            self.module._source_sha256(top, bottom), value["source_sha256"]
        )
        self.assertNotIn("top_text", value)
        self.assertNotIn("bottom_text", value)

    def test_resolve_reuses_only_validated_cached_indices(self):
        top, bottom = "广州健康创业圈子", "评论区扣888"
        value = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": self.module._source_sha256(top, bottom),
            "top1_end": len(top) - 1,
            "top_break_after": [], "bottom_break_after": [],
        }
        validator = mock.Mock(return_value=(True, {"ok": True}))
        with mock.patch.object(self.module, "generate", return_value=value) as generate:
            first, first_response = self.module.resolve(
                top, bottom, "ref-02-fixture-02", self.contract(), validator,
            )
            second, second_response = self.module.resolve(
                top, bottom, "ref-02-fixture-02", self.contract(), validator,
            )
        self.assertEqual((first, first_response), (second, second_response))
        generate.assert_called_once()
        self.assertEqual(2, validator.call_count)

    def test_number_tokens_filter_arabic_chinese_decimal_and_grouped_forms(self):
        for value, phrase in (
            ("团队8个人", "8个人"),
            ("团队8 个人", "8 个人"),
            ("产出100条短视频", "100条"),
            ("产出100 条短视频", "100 条"),
            ("团队十二个人", "十二个人"),
            ("团队一百个人", "一百个人"),
            ("覆盖3.5万人", "3.5万人"),
            ("产出1,000条视频", "1,000条"),
        ):
            with self.subTest(value=value):
                start = value.index(phrase)
                end = start + len(phrase)
                breaks = self.module._normalize_breaks(
                    list(range(len(value) - 1)), value,
                )
                if start:
                    self.assertIn(start - 1, breaks)
                for protected in range(start, end - 1):
                    self.assertNotIn(protected, breaks)

    def test_numeric_lists_keep_non_grouping_comma_boundaries(self):
        for value in (
            "2025，2026",
            "1，2，3个方案",
        ):
            with self.subTest(value=value):
                commas = [
                    index for index, char in enumerate(value) if char == "，"
                ]
                breaks = self.module._normalize_breaks(
                    list(range(len(value) - 1)), value,
                )
                self.assertTrue(commas)
                self.assertTrue(all(index in breaks for index in commas))

    def test_single_flight_covers_generation_repair_and_validation(self):
        top, bottom = "团队8个人，每天产出100条短视频", "评论区扣888"
        first = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": self.module._source_sha256(top, bottom),
            "top1_end": 1, "top_break_after": [1],
            "bottom_break_after": [],
        }
        repaired = dict(first, top1_end=top.index("，"),
                        top_break_after=[top.index("，")])
        for workers in (2, 5):
            with self.subTest(workers=workers):
                self.module._CACHE.clear()

                def generated(_top, _bottom, _contract, *, previous=None,
                              feedback=""):
                    return repaired if previous is not None and feedback else first

                def validator(value):
                    return (
                        (False, "HyperFrames top1 语义边界无效")
                        if value == first else (True, {"ok": True})
                    )

                validator_mock = mock.Mock(side_effect=validator)
                with mock.patch.object(
                    self.module, "generate", side_effect=generated,
                ) as generate, concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers,
                ) as pool:
                    futures = [
                        pool.submit(
                            self.module.resolve, top, bottom,
                            "ref-02-fixture-02", self.contract(), validator_mock,
                        )
                        for _ in range(workers)
                    ]
                    results = [future.result() for future in futures]
                self.assertTrue(all(item[0] == repaired for item in results))
                self.assertEqual(2, generate.call_count)


if __name__ == "__main__":
    unittest.main()
