from __future__ import annotations

import importlib
import concurrent.futures
import http.client
import io
import json
import ssl
import sys
import unittest
import urllib.error
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

    def test_prompt_exposes_layer_specific_effective_width(self):
        contract = self.v05_contract()
        contract["layers"]["bottom2"]["max_width_px"] = 862
        prompt = self.module._prompt(
            "团队8个人，每天产出100条短视频",
            "评论区扣111",
            contract,
        )
        self.assertIn("bottom2: 70px，可用宽 862px", prompt)
        self.assertIn("不确定时宁可选更早的完整语义边界", prompt)

    def test_top1_normalization_uses_nearest_boundary_and_prefers_earlier_tie(self):
        self.assertEqual(
            4,
            self.module._nearest_safe_top1_end(5, [4, 6], "一二三四五六七八"),
        )

    def test_resolve_rebalances_to_earlier_ai_boundary_before_second_ai_call(self):
        top = (
            "我在广州 组了一个健康赛道创业者的圈子 "
            "每天线上交流AI、流量、爆款项目、源头供应链"
        )
        bottom = "评论区扣888"
        first_boundary = top.index(" ")
        current_boundary = top.index(" ", first_boundary + 1)
        value = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": self.module._source_sha256(top, bottom),
            "top1_end": current_boundary,
            "top_break_after": [
                first_boundary, current_boundary - 1, current_boundary,
            ],
            "bottom_break_after": [],
        }

        def validator(candidate):
            if candidate["top1_end"] == first_boundary:
                return True, {"ok": True}
            return False, "HyperFrames 文案无法在完整语义边界内排入模板"

        validator_mock = mock.Mock(side_effect=validator)
        with mock.patch.object(
            self.module, "generate", return_value=value,
        ) as generate:
            result, response = self.module.resolve(
                top, bottom, "ref-04-fixture-04", self.v05_contract(),
                validator_mock,
            )
        self.assertEqual(first_boundary, result["top1_end"])
        self.assertEqual({"ok": True}, response)
        generate.assert_called_once()
        self.assertEqual(2, validator_mock.call_count)

    def test_resolve_rebalances_three_layer_copy_at_complete_phrase(self):
        top = (
            "我是大鹏 陕西西安人 在广州有个健康赛道创业圈子 "
            "资源共享|大健康|AI矩阵社交破圈|一人公司"
        )
        bottom = "评论区扣111"
        spaces = [index for index, char in enumerate(top) if char == " "]
        target, current = spaces[1], spaces[2]
        value = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": self.module._source_sha256(top, bottom),
            "top1_end": current,
            "top_break_after": [spaces[0], target, current - 1, current],
            "bottom_break_after": [],
        }
        validator = mock.Mock(side_effect=lambda candidate: (
            (True, {"ok": True}) if candidate["top1_end"] == target
            else (False, "HyperFrames 文案无法在完整语义边界内排入模板")
        ))
        with mock.patch.object(
            self.module, "generate", return_value=value,
        ) as generate:
            result, _response = self.module.resolve(
                top, bottom, "ref-05-fixture-05", self.v05_contract(),
                validator,
            )
        self.assertEqual(target, result["top1_end"])
        generate.assert_called_once()
        self.assertEqual(2, validator.call_count)

    def test_repair_feedback_identifies_longest_blocks_and_protects_words(self):
        top = (
            "我是大鹏 陕西西安人在广州有个健康赛道创业圈子"
            "资源共享|大健康|AI矩阵社交破圈|一人公司"
        )
        bottom = "PL区扣888"
        value = {
            "top_break_after": [3, 4, 9, 22, 27, 31, 40],
            "bottom_break_after": [],
        }
        feedback = self.module._repair_feedback(
            value, "HyperFrames 无法排入", top, bottom,
        )
        self.assertIn("顶部最长块为索引 10-22", feedback)
        self.assertIn("'在广州有个健康赛道创业圈子'", feedback)
        self.assertIn("不得原样重复", feedback)
        self.assertIn("严禁拆开任何双字词", feedback)
        self.assertIn("top2/top3", feedback)

    def test_repair_prompt_uses_generic_exact_semantic_chunks(self):
        top = "老周在上海组建了品牌运营团队"
        bottom = "评论区留言"
        prompt = self.module._repair_prompt(
            top, bottom, self.v05_contract(),
            {"top1_end": 1, "top_break_after": [1]},
            "真实字体校验失败",
        )
        self.assertIn("top_chunks", prompt)
        self.assertIn("数组连接后必须逐字等于原文", prompt)
        self.assertIn("先拆主语、地点状语、谓语、宾语和并列项", prompt)
        self.assertFalse(hasattr(self.module, "_DOMAIN_PROTECTED_PHRASES"))

    def test_chunk_layout_restores_only_omitted_whitespace(self):
        top = (
            "我是大鹏 陕西西安人在广州有个健康赛道创业圈子"
            "资源共享|大健康|AI矩阵社交破圈|一人公司"
        )
        bottom = "PL区扣888"
        raw = {
            "top_chunks": [
                "我是大鹏", "陕西西安人", "在广州", "有个", "健康赛道",
                "创业圈子", "资源共享|", "大健康|", "AI矩阵",
                "社交破圈|", "一人公司",
            ],
            "bottom_chunks": ["PL区", "扣888"],
            "top1_chunk_count": 2,
        }
        value = self.module._chunk_layout(raw, top, bottom, "gpt-4.1")
        self.assertEqual(9, value["top1_end"])
        self.assertEqual(
            [4, 9, 12, 14, 18, 22, 27, 31, 35, 40],
            value["top_break_after"],
        )
        self.assertEqual([2], value["bottom_break_after"])
        self.assertEqual("gpt-4.1", value["model"])

    def test_chunk_layout_rejects_any_non_whitespace_rewrite(self):
        for chunks in (
            ["我是大鹏", "陕西西安人"],
            ["我是大鹏 ", "陕西西安人!"],
        ):
            with self.subTest(chunks=chunks), self.assertRaisesRegex(
                RuntimeError, "改写|未覆盖",
            ):
                self.module._chunk_layout({
                    "top_chunks": chunks,
                    "bottom_chunks": ["PL区扣888"],
                    "top1_chunk_count": 1,
                }, "我是大鹏 陕西西安人在广州", "PL区扣888", "gpt-4.1")

    def test_generate_repair_converts_dynamic_chunks_to_source_indices(self):
        top = "老周在上海组建了品牌运营团队|每天交流项目"
        bottom = "评论区留言"
        raw = {
            "top_chunks": [
                "老周", "在上海", "组建了", "品牌运营", "团队|", "每天", "交流项目",
            ],
            "bottom_chunks": ["评论区", "留言"],
            "top1_chunk_count": 2,
        }
        with mock.patch.object(
            self.module, "_request", return_value=raw,
        ) as request:
            value = self.module.generate(
                top, bottom, self.v05_contract(), previous={"top1_end": 1},
                feedback="排版失败", model="gpt-4.1", repair=True,
            )
        self.assertEqual("gpt-4.1", value["model"])
        self.assertEqual(len("老周在上海") - 1, value["top1_end"])
        self.assertEqual([2], value["bottom_break_after"])
        self.assertTrue(request.call_args.kwargs["repair"])

    def test_failed_mini_candidate_uses_stronger_repair_model(self):
        top = (
            "我是大鹏 陕西西安人在广州有个健康赛道创业圈子"
            "资源共享|大健康|AI矩阵社交破圈|一人公司"
        )
        bottom = "PL区扣888"
        first = {
            "version": 1, "model": "gpt-4.1-mini",
            "source_sha256": self.module._source_sha256(top, bottom),
            "top1_end": 4,
            "top_break_after": [3, 4, 9, 22, 27, 31, 40],
            "bottom_break_after": [],
        }
        repaired = {
            **first, "model": "gpt-4.1", "top1_end": 9,
            "top_break_after": [3, 4, 9, 12, 22, 27, 31, 40],
        }
        calls = []

        def generated(_top, _bottom, _contract, *, previous=None,
                      feedback="", model=None, repair=False):
            calls.append({
                "previous": previous, "feedback": feedback, "model": model,
                "repair": repair,
            })
            return first if previous is None else repaired

        validator = mock.Mock(side_effect=lambda value: (
            (True, {"ok": True}) if value == repaired
            else (False, "HyperFrames 文案无法在完整语义边界内排入模板")
        ))
        with mock.patch.object(
            self.module, "generate", side_effect=generated,
        ):
            result, response = self.module.resolve(
                top, bottom, "ref-05-changsha-white-red",
                self.v05_contract(), validator,
            )
        self.assertEqual(repaired, result)
        self.assertEqual({"ok": True}, response)
        self.assertEqual(
            [self.module.MODEL, self.module.REPAIR_MODEL],
            [item["model"] for item in calls],
        )
        self.assertEqual([False, True], [item["repair"] for item in calls])
        self.assertIn("顶部最长块为索引 10-22", calls[1]["feedback"])
        self.assertEqual(first, calls[1]["previous"])
        self.assertEqual(2, validator.call_count)

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
             mock.patch.object(
                 self.module.urllib.request, "urlopen", return_value=fake,
             ) as urlopen:
            value = self.module.generate(
                top, bottom, self.contract(), model="gpt-4.1",
            )
        self.assertEqual(1, value["top1_end"])
        self.assertEqual("gpt-4.1", value["model"])
        request_body = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual("gpt-4.1", request_body["model"])
        self.assertIn(1, value["top_break_after"])
        self.assertEqual(
            self.module._source_sha256(top, bottom), value["source_sha256"]
        )
        self.assertNotIn("top_text", value)
        self.assertNotIn("bottom_text", value)

    @staticmethod
    def openai_response(content):
        response = {
            "choices": [{"message": {"content": json.dumps(content)}}],
        }
        fake = mock.MagicMock()
        fake.__enter__.return_value = io.BytesIO(json.dumps(response).encode())
        fake.__exit__.return_value = False
        return fake

    def test_request_retries_remote_disconnect_then_succeeds(self):
        fake = self.openai_response({"ok": True})
        with mock.patch.object(self.module, "OPENAI_KEY", "configured"), \
             mock.patch.object(
                 self.module.urllib.request, "urlopen", side_effect=[
                     http.client.RemoteDisconnected("temporary disconnect"),
                     http.client.RemoteDisconnected("temporary disconnect"),
                     fake,
                 ],
             ) as urlopen, mock.patch.object(
                 self.module.time, "sleep",
             ) as sleep:
            result = self.module._request(
                "顶部文案", "底部文案", self.contract(),
            )
        self.assertEqual({"ok": True}, result)
        self.assertEqual(3, urlopen.call_count)
        self.assertEqual([mock.call(1), mock.call(2)], sleep.call_args_list)
        self.assertTrue(all(
            call.kwargs["timeout"] == 15 for call in urlopen.call_args_list
        ))

    def test_request_retries_response_body_transport_failures(self):
        failures = (
            http.client.IncompleteRead(b'{"choices":', 10),
            ConnectionResetError("connection reset"),
            ssl.SSLEOFError(8, "unexpected EOF"),
            TimeoutError("response timeout"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                broken = mock.MagicMock()
                reader = mock.MagicMock()
                reader.read.side_effect = failure
                broken.__enter__.return_value = reader
                broken.__exit__.return_value = False
                with mock.patch.object(
                    self.module, "OPENAI_KEY", "configured",
                ), mock.patch.object(
                    self.module.urllib.request, "urlopen",
                    side_effect=[broken, self.openai_response({"ok": True})],
                ) as urlopen, mock.patch.object(
                    self.module.time, "sleep",
                ) as sleep:
                    result = self.module._request(
                        "顶部文案", "底部文案", self.contract(),
                    )
                self.assertEqual({"ok": True}, result)
                self.assertEqual(2, urlopen.call_count)
                self.assertEqual([mock.call(1)], sleep.call_args_list)

    def test_request_stops_after_three_connection_failures(self):
        with mock.patch.object(self.module, "OPENAI_KEY", "configured"), \
             mock.patch.object(
                 self.module.urllib.request, "urlopen",
                 side_effect=urllib.error.URLError("offline"),
             ) as urlopen, mock.patch.object(
                 self.module.time, "sleep",
             ) as sleep, self.assertRaisesRegex(
                 RuntimeError, "AI 语义排版服务连接失败",
             ):
            self.module._request("顶部文案", "底部文案", self.contract())
        self.assertEqual(3, urlopen.call_count)
        self.assertEqual([mock.call(1), mock.call(2)], sleep.call_args_list)

    def test_request_does_not_retry_http_errors(self):
        for status in (400, 500):
            error = urllib.error.HTTPError(
                "https://api.openai.com/v1/chat/completions",
                status, "HTTP failure", {}, io.BytesIO(),
            )
            with self.subTest(status=status), mock.patch.object(
                self.module, "OPENAI_KEY", "configured",
            ), mock.patch.object(
                self.module.urllib.request, "urlopen", side_effect=error,
            ) as urlopen, mock.patch.object(
                self.module.time, "sleep",
            ) as sleep, self.assertRaisesRegex(
                RuntimeError, f"HTTP {status}",
            ):
                self.module._request("顶部文案", "底部文案", self.contract())
            self.assertEqual(1, urlopen.call_count)
            sleep.assert_not_called()

    def test_request_does_not_retry_invalid_json(self):
        invalid = mock.MagicMock()
        invalid.__enter__.return_value = io.BytesIO(b"{not-json")
        invalid.__exit__.return_value = False
        with mock.patch.object(self.module, "OPENAI_KEY", "configured"), \
             mock.patch.object(
                 self.module.urllib.request, "urlopen", return_value=invalid,
             ) as urlopen, mock.patch.object(
                 self.module.time, "sleep",
             ) as sleep, self.assertRaisesRegex(
                 RuntimeError, "AI 语义排版返回无效",
             ):
            self.module._request("顶部文案", "底部文案", self.contract())
        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()

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
                              feedback="", model=None, repair=False):
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
