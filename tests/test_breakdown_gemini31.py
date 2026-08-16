# -*- coding: utf-8 -*-
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from contextlib import closing
from unittest import mock


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))
from content_domains import breakdown, gemini_reverse


def _shot(index, window, suffix=""):
    start, end, _label = window
    middle = round((start + end) / 2.0, 3)
    visible = (
        "白色矩形位于蓝色背景中央并保持静止",
        "红色圆形从画面左侧匀速移动到右侧",
        "蓝色三角形由近景向后景逐渐缩小",
        "绿色竖线从低机位画面底部向上延伸",
    )[(index - 1) % 4]
    rows = []
    for key in gemini_reverse.FACT_FIELDS:
        if key in gemini_reverse.OPTIONAL_FACT_FIELDS:
            value = gemini_reverse.NOT_APPLICABLE
            evidence = []
        else:
            value = "%s：%s；%s" % (key, visible, suffix or "证据清晰")
            evidence = [middle]
        if key == "action_start":
            evidence = [start]
        elif key == "action_end":
            evidence = [end]
        rows.append({
            "key": key,
            "value": value,
            "evidence_seconds": evidence,
        })
    return {
        "segment_id": index,
        "transition_from_previous": (
            {
                "type": "none",
                "description": gemini_reverse.NOT_APPLICABLE,
                "evidence_seconds": [],
            }
            if index == 1 else {
                "type": "hard_cut",
                "description": "画面在服务器边界处直接切换",
                "evidence_seconds": [start],
            }
        ),
        "facts": rows,
        "generation_advice": {
            "aspect_ratio": "保持原片竖屏画幅",
            "fps": "二十四帧每秒",
            "camera_control": "按可见机位稳定执行第%d段" % index,
            "negative_prompt": "禁止新增人物道具文字和无证据动作",
        },
    }


def _payload(windows, suffix=""):
    return {
        "shots": [
            _shot(index, window, suffix=suffix)
            for index, window in enumerate(windows, 1)
        ],
    }


def _response(payload, finish_reason="STOP"):
    return {
        "candidates": [{
            "finishReason": finish_reason,
            "content": {"parts": [{
                "text": json.dumps(payload, ensure_ascii=False),
            }]},
        }],
    }


class GeminiReverseSchemaTests(unittest.TestCase):
    def test_model_and_live_request_contract_are_fixed(self):
        windows = gemini_reverse.fixed_windows(12.0)
        body = gemini_reverse._request_body(
            {"inline_data": {"mime_type": "video/mp4", "data": "AA=="}},
            "title",
            12.0,
            "local",
            "",
            windows,
        )
        config = body["generationConfig"]
        self.assertEqual(gemini_reverse.MODEL, "gemini-3.1-pro-preview")
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertEqual(config["thinkingConfig"], {"thinkingLevel": "medium"})
        self.assertEqual(config["maxOutputTokens"], 32768)
        self.assertIn("responseJsonSchema", config)
        self.assertNotIn("responseSchema", config)
        self.assertNotIn("start_seconds", json.dumps(config))

    def test_provider_schema_only_drops_live_incompatible_array_bounds(self):
        full = gemini_reverse._schema()
        provider = gemini_reverse.provider_schema()
        self.assertIn("minItems", full["properties"]["shots"])
        encoded = json.dumps(provider)
        self.assertNotIn("minItems", encoded)
        self.assertNotIn("maxItems", encoded)
        self.assertIn("additionalProperties", encoded)
        self.assertIn("evidence_seconds", encoded)

    def test_one_to_four_server_windows_parse_without_model_timeline_fields(self):
        for duration in (0.4, 4.0, 9.0, 16.0):
            windows = gemini_reverse.fixed_windows(duration)
            parsed = gemini_reverse.parse_result(
                json.dumps(_payload(windows), ensure_ascii=False),
                windows,
            )
            self.assertEqual(len(parsed), len(windows))
            self.assertEqual(parsed[0]["start_seconds"], 0.0)
            self.assertAlmostEqual(parsed[-1]["end_seconds"], duration, places=3)

    def test_truncated_or_wrapped_json_is_rejected_without_salvage(self):
        windows = gemini_reverse.fixed_windows(4.0)
        valid = json.dumps(_payload(windows), ensure_ascii=False)
        for raw in (valid[:-1], "```json\n" + valid + "\n```", "prefix " + valid):
            with self.assertRaisesRegex(ValueError, "not complete JSON"):
                gemini_reverse.parse_result(raw, windows)

    def test_missing_duplicate_and_empty_fact_rows_are_rejected(self):
        windows = gemini_reverse.fixed_windows(4.0)
        for mutate, expected in (
            (lambda rows: rows.pop(), "恰好包含"),
            (lambda rows: rows.__setitem__(1, dict(rows[0])), "缺失、重复"),
            (lambda rows: rows[0].__setitem__("value", ""), "为空"),
        ):
            payload = _payload(windows)
            mutate(payload["shots"][0]["facts"])
            with self.assertRaisesRegex(ValueError, expected):
                gemini_reverse.parse_result(
                    json.dumps(payload, ensure_ascii=False),
                    windows,
                )

    def test_subjective_visual_claim_is_rejected_with_segment_and_word(self):
        windows = gemini_reverse.fixed_windows(4.0)
        payload = _payload(windows)
        payload["shots"][0]["facts"][0]["value"] = "画面里似乎是一名演员"
        with self.assertRaisesRegex(ValueError, "第1段.*似乎"):
            gemini_reverse.parse_result(
                json.dumps(payload, ensure_ascii=False),
                windows,
            )

    def test_evidence_must_be_inside_window_and_bind_action_endpoints(self):
        windows = gemini_reverse.fixed_windows(4.0)
        payload = _payload(windows)
        payload["shots"][0]["facts"][0]["evidence_seconds"] = [99]
        with self.assertRaisesRegex(ValueError, "超出服务器区间"):
            gemini_reverse.parse_result(
                json.dumps(payload, ensure_ascii=False), windows
            )

        payload = _payload(windows)
        action_start = gemini_reverse.FACT_FIELDS.index("action_start")
        payload["shots"][0]["facts"][action_start]["evidence_seconds"] = [
            windows[0][1],
        ]
        with self.assertRaisesRegex(ValueError, "action_start缺少起点证据"):
            gemini_reverse.parse_result(
                json.dumps(payload, ensure_ascii=False), windows
            )

    def test_unknown_slots_cannot_pass_readiness_by_padding(self):
        windows = gemini_reverse.fixed_windows(4.0)
        payload = _payload(windows)
        for row in payload["shots"][0]["facts"][:4]:
            row["value"] = gemini_reverse.UNKNOWN
            row["evidence_seconds"] = []
        with self.assertRaisesRegex(ValueError, "生成就绪度不足90%"):
            gemini_reverse.parse_result(
                json.dumps(payload, ensure_ascii=False), windows
            )

    def test_repeated_segments_are_rejected_without_annotation_or_expansion(self):
        windows = gemini_reverse.fixed_windows(16.0)
        payload = _payload(windows)
        payload["shots"][1]["facts"] = json.loads(json.dumps(
            payload["shots"][0]["facts"], ensure_ascii=False
        ))
        for row in payload["shots"][1]["facts"]:
            if row["evidence_seconds"]:
                row["evidence_seconds"] = [
                    windows[1][0]
                    if row["key"] == "action_start"
                    else windows[1][1]
                    if row["key"] == "action_end"
                    else round(sum(windows[1][:2]) / 2.0, 3)
                ]
        with self.assertRaisesRegex(ValueError, "第2段与第1段内容重复"):
            gemini_reverse.parse_result(
                json.dumps(payload, ensure_ascii=False), windows
            )

    def test_shared_subject_and_scene_are_allowed_when_action_is_distinct(self):
        windows = gemini_reverse.fixed_windows(16.0)
        payload = _payload(windows)
        first = payload["shots"][0]["facts"]
        second = payload["shots"][1]["facts"]
        action_keys = {
            "action_start", "action_process", "action_end", "direction_speed",
        }
        for index, row in enumerate(second):
            if row["key"] in action_keys:
                continue
            row["value"] = first[index]["value"]
        parsed = gemini_reverse.parse_result(
            json.dumps(payload, ensure_ascii=False), windows
        )
        self.assertEqual(len(parsed), len(windows))

    def test_prompt_contains_all_generation_sections_and_server_ranges(self):
        windows = gemini_reverse.fixed_windows(4.0)
        entries = gemini_reverse.parse_result(
            json.dumps(_payload(windows), ensure_ascii=False), windows
        )
        prompt = gemini_reverse.assemble_prompt(entries)
        for label in (
            "主体：", "动作：", "场景：", "构图：", "光影：", "风格：",
            "节奏：", "生成建议：",
        ):
            self.assertIn(label, prompt)
        self.assertTrue(prompt.startswith(windows[0][2]))
        self.assertNotIn("unknown", prompt)
        self.assertNotIn("not_applicable", prompt)

    def test_authoritative_timeline_is_deterministic_gap_free_and_tenth_precision(self):
        candidates = [
            {"at_seconds": 8.24, "score": 0.91},
            {"at_seconds": 15.58, "score": 0.88},
        ]
        expected = breakdown._build_authoritative_reverse_timeline(
            21.534, candidates,
        )
        for _index in range(100):
            self.assertEqual(
                breakdown._build_authoritative_reverse_timeline(
                    21.534, candidates,
                ),
                expected,
            )
        self.assertEqual(expected["precision_seconds"], 0.1)
        self.assertEqual(
            expected["windows"],
            [
                (0.0, 8.2, "[00:00.0-00:08.2]"),
                (8.2, 15.6, "[00:08.2-00:15.6]"),
                (15.6, 21.5, "[00:15.6-00:21.5]"),
            ],
        )
        self.assertEqual(expected["windows"][0][0], 0.0)
        self.assertEqual(expected["windows"][-1][1], 21.5)

    def test_authoritative_timeline_uses_only_strong_spaced_candidates(self):
        timeline = breakdown._build_authoritative_reverse_timeline(
            10.0,
            [
                {"at_seconds": 0.4, "score": 0.99},
                {"at_seconds": 2.0, "score": 0.70},
                {"at_seconds": 2.4, "score": 0.95},
                {"at_seconds": 5.0, "score": 0.90},
                {"at_seconds": 8.0, "score": 0.80},
                {"at_seconds": 9.6, "score": 0.99},
            ],
        )
        self.assertEqual(
            [item["at_seconds"] for item in timeline["transitions"]],
            [2.4, 5.0, 8.0],
        )
        self.assertEqual(len(timeline["windows"]), 4)
        for previous, current in zip(
            timeline["windows"], timeline["windows"][1:],
        ):
            self.assertEqual(previous[1], current[0])

    def test_ffmpeg_candidates_are_evidence_only_and_path_is_not_audited(self):
        output = (
            "[Parsed_metadata_1] frame:0 pts:824 pts_time:8.24\n"
            "[Parsed_metadata_1] lavfi.scene_score=0.812345\n"
            "[Parsed_metadata_1] frame:1 pts:1558 pts_time:15.58\n"
            "[Parsed_metadata_1] lavfi.scene_score=0.723456\n"
        )
        completed = mock.Mock(returncode=0, stdout="", stderr=output)
        with mock.patch.object(
            breakdown.subprocess, "run", return_value=completed,
        ):
            candidates, audit = breakdown._detect_reverse_transition_candidates(
                "private-video.mp4", 21.534,
            )
        self.assertEqual(
            [item["at_seconds"] for item in candidates], [8.2, 15.6],
        )
        self.assertEqual(audit["candidate_count"], 2)
        self.assertNotIn("private-video.mp4", json.dumps(audit))

    def test_detector_failure_falls_back_to_one_server_owned_segment(self):
        with mock.patch.object(
            breakdown.subprocess, "run", side_effect=FileNotFoundError("ffmpeg"),
        ):
            timeline = breakdown._authoritative_reverse_timeline(
                "private-video.mp4", 21.534,
            )
        self.assertEqual(
            timeline["windows"], [(0.0, 21.5, "[00:00.0-00:21.5]")],
        )
        self.assertEqual(timeline["detector_audit"]["status"], "unavailable")

    def test_real_pts_own_unequal_windows_and_reference_indices_are_explicit(self):
        frames = ["frame-%d.jpg" % index for index in range(1, 9)]
        windows = [
            (0.0, 1.0, "[00:00.0-00:01.0]"),
            (1.0, 9.0, "[00:01.0-00:09.0]"),
            (9.0, 10.0, "[00:09.0-00:10.0]"),
        ]
        points = [0.0, 1.25, 2.5, 3.75, 5.0, 6.25, 8.75, 9.5]
        bundle = breakdown._reverse_frame_bundle(
            frames, windows, frame_pts=points,
        )
        self.assertEqual(
            bundle["segment_source_indices"],
            [[1], [2, 3, 4, 5, 6, 7], [8]],
        )
        self.assertEqual(
            bundle["segment_model_source_indices"], [[1], [2, 7], [8]],
        )
        self.assertEqual(bundle["reference_thumbnail_indices"], [1, 2, 3])
        self.assertEqual(
            [item["source_frame_index"] for item in bundle["manifest"][:3]],
            [1, 7, 8],
        )

    def test_model_cannot_return_timestamps_or_unbound_transition(self):
        windows = [(0.0, 1.0, "[00:00.0-00:01.0]")]
        payload = _payload(windows)
        payload["shots"][0]["start_seconds"] = 0.0
        with self.assertRaisesRegex(ValueError, "结构字段不完整"):
            gemini_reverse.parse_result(
                json.dumps(payload, ensure_ascii=False), windows,
            )

        windows = [
            (0.0, 1.0, "[00:00.0-00:01.0]"),
            (1.0, 2.0, "[00:01.0-00:02.0]"),
        ]
        payload = _payload(windows)
        payload["shots"][1]["transition_from_previous"]["evidence_seconds"] = [3.0]
        with self.assertRaisesRegex(ValueError, "超出服务器边界"):
            gemini_reverse.parse_result(
                json.dumps(payload, ensure_ascii=False), windows,
            )

    def test_transition_is_bound_to_server_boundary_and_rendered(self):
        windows = [
            (0.0, 1.0, "[00:00.0-00:01.0]"),
            (1.0, 2.0, "[00:01.0-00:02.0]"),
        ]
        entries = gemini_reverse.parse_result(
            json.dumps(_payload(windows), ensure_ascii=False), windows,
        )
        transition = entries[1]["transition_from_previous"]
        self.assertEqual(transition["boundary_id"], 1)
        self.assertEqual(transition["at_seconds"], 1.0)
        self.assertEqual(transition["time_source"], "server_ffmpeg")
        self.assertEqual(transition["type_source"], "gemini")
        prompt = gemini_reverse.assemble_prompt(entries)
        self.assertIn("转场：直接硬切", prompt)
        self.assertIn("[00:00.0-00:01.0]", prompt)
        self.assertIn("[00:01.0-00:02.0]", prompt)

    def test_quality_total_is_minimum_and_never_legacy_hundred_zero(self):
        windows = [(0.0, 1.0, "[00:00.0-00:01.0]")]
        entries = gemini_reverse.parse_result(
            json.dumps(_payload(windows), ensure_ascii=False), windows,
        )
        score = gemini_reverse.quality_score(entries)
        self.assertFalse(score["legacy_unstructured"])
        self.assertGreaterEqual(score["components"]["generation_readiness"], 90)
        self.assertEqual(score["components"]["factual_consistency"], 100.0)
        self.assertEqual(score["total"], min(score["components"].values()))


class GeminiReverseRequestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        temp.write(b"video")
        temp.close()
        self.path = temp.name

    def tearDown(self):
        pathlib.Path(self.path).unlink(missing_ok=True)

    def _analyze(self, side_effect):
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), \
             mock.patch.object(
                 gemini_reverse,
                 "_media_part",
                 return_value=({
                     "inline_data": {"mime_type": "video/mp4", "data": "dm"},
                 }, None),
             ), \
             mock.patch.object(
                 gemini_reverse,
                 "_json_request",
                 side_effect=side_effect,
             ) as request:
            result = gemini_reverse.analyze_video(
                self.path,
                "video/mp4",
                "title",
                4.0,
                "local",
                "",
            )
        return result, request

    def test_validation_failure_retries_once_with_original_media_only(self):
        windows = gemini_reverse.fixed_windows(4.0)
        invalid = _payload(windows)
        invalid["shots"][0]["facts"][0]["value"] = "似乎是一名演员"
        result, request = self._analyze([
            _response(invalid),
            _response(_payload(windows, suffix="重试")),
        ])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["attempts"], 2)
        self.assertFalse(result["cross_provider_fallback"])
        self.assertIn(
            "/v1beta/models/gemini-3.1-pro-preview:generateContent",
            request.call_args.args[0],
        )
        first_media = request.call_args_list[0].args[1]["contents"][0]["parts"][0]
        second_media = request.call_args_list[1].args[1]["contents"][0]["parts"][0]
        self.assertEqual(first_media, second_media)
        second_instruction = request.call_args_list[1].args[1]["contents"][0]["parts"][1]["text"]
        self.assertIn("failed strict validation", second_instruction)
        self.assertNotIn("似乎是一名演员", second_instruction)

    def test_two_invalid_outputs_fail_without_salvage_or_provider_fallback(self):
        windows = gemini_reverse.fixed_windows(4.0)
        invalid = _payload(windows)
        invalid["shots"][0]["facts"][0]["value"] = ""
        with self.assertRaisesRegex(ValueError, "校验失败.*为空"):
            self._analyze([_response(invalid), _response(invalid)])

    def test_max_tokens_finish_reason_is_validation_failure(self):
        windows = gemini_reverse.fixed_windows(4.0)
        with self.assertRaisesRegex(ValueError, "MAX_TOKENS"):
            self._analyze([
                _response(_payload(windows), finish_reason="MAX_TOKENS"),
                _response(_payload(windows), finish_reason="MAX_TOKENS"),
            ])

    def test_missing_key_fails_before_provider_or_file_read(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(gemini_reverse, "_media_part") as media:
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                gemini_reverse.analyze_video(
                    self.path, "video/mp4", "", 4.0, "local", ""
                )
        media.assert_not_called()

    def test_audit_never_logs_raw_prompt_url_or_credential(self):
        windows = gemini_reverse.fixed_windows(4.0)
        invalid = _payload(windows)
        invalid["shots"][0]["facts"][0]["value"] = "似乎包含 https://private.example/x"
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            result, _request = self._analyze([
                _response(invalid),
                _response(_payload(windows, suffix="安全")),
            ])
        logged = output.getvalue()
        self.assertNotIn("private.example", logged)
        self.assertNotIn("test-key", logged)
        self.assertIn("response_sha256", logged)
        self.assertEqual(result["attempts"], 2)

    def test_request_failure_audit_is_redacted_and_does_not_validation_retry(self):
        output = io.StringIO()
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), \
             mock.patch.object(
                 gemini_reverse,
                 "_media_part",
                 return_value=({
                     "inline_data": {"mime_type": "video/mp4", "data": "dm"},
                 }, None),
             ), \
             mock.patch.object(
                 gemini_reverse,
                 "_json_request",
                 side_effect=RuntimeError(
                     "Gemini HTTP 400: INVALID_ARGUMENT: "
                     "token=secret-value https://private.example/x"
                 ),
             ) as request, \
             mock.patch("sys.stdout", output):
            with self.assertRaisesRegex(RuntimeError, "INVALID_ARGUMENT"):
                gemini_reverse.analyze_video(
                    self.path, "video/mp4", "", 4.0, "local", ""
                )
        self.assertEqual(request.call_count, 1)
        logged = output.getvalue()
        self.assertIn('"http_status": 400', logged)
        self.assertNotIn("secret-value", logged)
        self.assertNotIn("private.example", logged)

    def test_uploaded_media_is_deleted_when_processing_poll_fails(self):
        uploaded = {
            "name": "files/test-media",
            "uri": "https://generativelanguage.googleapis.com/file/test-media",
            "mime_type": "video/mp4",
        }
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), \
             mock.patch.object(
                 gemini_reverse,
                 "_media_part",
                 return_value=({
                     "file_data": {
                         "mime_type": "video/mp4",
                         "file_uri": uploaded["uri"],
                     },
                 }, uploaded),
             ), \
             mock.patch.object(
                 gemini_reverse,
                 "_wait_for_file",
                 side_effect=TimeoutError("poll timeout"),
             ), \
             mock.patch.object(gemini_reverse, "_delete_file") as deleted:
            with self.assertRaisesRegex(TimeoutError, "poll timeout"):
                gemini_reverse.analyze_video(
                    self.path, "video/mp4", "", 16.0, "local", ""
                )
        deleted.assert_called_once_with(
            uploaded,
            "test-key",
            cleanup_jdb=None,
        )

    def test_runtime_integration_uses_gemini_and_exposes_only_audit_summary(self):
        gemini_result = {
            "provider": "google",
            "model": gemini_reverse.MODEL,
            "attempts": 1,
            "prompt": "[00:00-00:04] 主体：白色矩形",
            "attempt_audit": [{"attempt": 1, "validation": "passed"}],
            "timeline_audit": {
                "windows": [(0.0, 4.0, "[00:00.0-00:04.0]")],
                "source": "single_full_media_segment",
            },
            "quality_score": {
                "total": 100.0,
                "components": {
                    "source_evidence_coverage": 100.0,
                    "generation_readiness": 100.0,
                    "factual_consistency": 100.0,
                },
                "legacy_unstructured": False,
            },
            "entries": [{
                "segment_id": 1,
                "start_seconds": 0.0,
                "end_seconds": 4.0,
                "readiness": {"ready": 17, "applicable": 17, "percent": 100.0},
                "transition_from_previous": {
                    "boundary_id": None,
                    "at_seconds": None,
                    "type": "none",
                    "description": gemini_reverse.NOT_APPLICABLE,
                    "evidence_seconds": [],
                    "time_source": "server_ffmpeg",
                    "type_source": "gemini",
                },
                "facts": {
                    key: {"value": "fact", "evidence_seconds": [0.0]}
                    for key in gemini_reverse.FACT_FIELDS
                },
            }],
        }
        with mock.patch.object(
            gemini_reverse, "analyze_video", return_value=gemini_result
        ) as analyze, mock.patch.object(
            breakdown, "_frame_thumbnails", return_value=["thumb"]
        ), mock.patch.object(
            breakdown,
            "_authoritative_reverse_timeline",
            return_value=gemini_result["timeline_audit"],
        ):
            result = breakdown._reverse_from_frames(
                {"_job_id": 7},
                ["frame.jpg"],
                duration=4.0,
                media_path=self.path,
            )
        analyze.assert_called_once()
        self.assertEqual(result["model_provider"], "google")
        self.assertEqual(result["model_id"], gemini_reverse.MODEL)
        self.assertEqual(result["prompt"], gemini_result["prompt"])
        self.assertNotIn("sections", result)
        self.assertFalse(
            result["reverse_audit"]["cross_provider_fallback"]
        )


class GeminiReverseHttpTests(unittest.TestCase):
    def _response_context(self, payload=b"", headers=None):
        response = mock.MagicMock()
        response.headers = headers or {}
        response.read.return_value = payload
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    def _http_error(self, code, payload=None):
        return urllib.error.HTTPError(
            "https://generativelanguage.googleapis.com/test",
            code,
            "error",
            {},
            io.BytesIO(json.dumps(payload or {}).encode("utf-8")),
        )

    def test_non_retryable_400_is_not_reissued(self):
        request = urllib.request.Request("https://example.invalid")
        error = self._http_error(400, {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "bad key=AIzaSECRET123 https://private.example/x",
            },
        })
        with mock.patch.object(
            gemini_reverse, "_open_url", side_effect=error
        ) as opened:
            with self.assertRaisesRegex(RuntimeError, "INVALID_ARGUMENT") as raised:
                gemini_reverse._open(request)
        self.assertEqual(opened.call_count, 1)
        self.assertNotIn("AIzaSECRET", str(raised.exception))
        self.assertNotIn("private.example", str(raised.exception))

    def test_429_retries_same_request_once(self):
        request = urllib.request.Request("https://example.invalid")
        response = mock.MagicMock()
        with mock.patch.object(
            gemini_reverse,
            "_open_url",
            side_effect=[self._http_error(429), response],
        ) as opened:
            got = gemini_reverse._open(request)
        self.assertIs(got, response)
        self.assertEqual(opened.call_count, 2)

    def test_transport_uses_the_selected_egress_proxy(self):
        request = urllib.request.Request("https://generativelanguage.googleapis.com/test")
        response = mock.MagicMock()
        opener = mock.MagicMock()
        opener.open.return_value = response
        with mock.patch.object(gemini_reverse.egress, "preferred_proxy", return_value="http://127.0.0.1:7999"), \
             mock.patch.object(urllib.request, "ProxyHandler") as proxy_handler, \
             mock.patch.object(urllib.request, "build_opener", return_value=opener):
            got = gemini_reverse._open_url(request, 20)
        self.assertIs(got, response)
        proxy_handler.assert_called_once_with({
            "http": "http://127.0.0.1:7999",
            "https": "http://127.0.0.1:7999",
        })
        opener.open.assert_called_once_with(request, timeout=20)

    def test_processing_longer_than_thirty_seconds_can_become_active(self):
        clock = [0.0]

        def monotonic():
            return clock[0]

        def sleep(seconds):
            clock[0] += seconds

        processing = json.dumps({"state": "PROCESSING"}).encode("utf-8")
        active = json.dumps({
            "state": "ACTIVE",
            "uri": "https://generativelanguage.googleapis.com/file/active",
        }).encode("utf-8")
        responses = [self._response_context(processing) for _ in range(6)]
        responses.append(self._response_context(active))
        file_info = {
            "name": "files/test-media",
            "uri": "https://generativelanguage.googleapis.com/file/pending",
            "mime_type": "video/mp4",
        }
        with mock.patch.object(gemini_reverse.time, "monotonic", side_effect=monotonic), \
             mock.patch.object(gemini_reverse.time, "sleep", side_effect=sleep), \
             mock.patch.object(gemini_reverse, "_open", side_effect=responses) as opened:
            result = gemini_reverse._wait_for_file(
                file_info,
                "test-key",
                deadline=100.0,
            )
        self.assertGreater(clock[0], 30.0)
        self.assertEqual(result["uri"], "https://generativelanguage.googleapis.com/file/active")
        self.assertEqual(opened.call_count, 7)

    def test_large_file_upload_is_chunked_with_offsets_and_finalization(self):
        start = self._response_context(headers={
            "X-Goog-Upload-URL": (
                "https://generativelanguage.googleapis.com/upload/session"
            ),
        })
        middle_one = self._response_context()
        middle_two = self._response_context()
        final = self._response_context(json.dumps({
            "file": {
                "name": "files/test-media",
                "uri": "https://generativelanguage.googleapis.com/file/test-media",
            },
        }).encode("utf-8"))
        with tempfile.NamedTemporaryFile(delete=False) as media:
            media.write(b"0123456789")
            path = media.name
        self.addCleanup(lambda: pathlib.Path(path).unlink(missing_ok=True))
        with mock.patch.object(gemini_reverse, "UPLOAD_CHUNK_BYTES", 4), \
             mock.patch.object(
                 gemini_reverse,
                 "_open",
                 side_effect=[start, middle_one, middle_two, final],
             ) as opened:
            result = gemini_reverse._upload_file(
                path,
                "video/mp4",
                "test-key",
                deadline=100.0,
            )
        requests = [call.args[0] for call in opened.call_args_list[1:]]
        self.assertEqual([len(request.data) for request in requests], [4, 4, 2])
        self.assertEqual(
            [request.get_header("X-goog-upload-offset") for request in requests],
            ["0", "4", "8"],
        )
        self.assertEqual(
            [request.get_header("X-goog-upload-command") for request in requests],
            ["upload", "upload", "upload, finalize"],
        )
        self.assertEqual(result["name"], "files/test-media")
        self.assertTrue(all(
            call.kwargs.get("retry_transient") is False
            for call in opened.call_args_list[1:]
        ))

    def test_delete_failure_is_retried_and_left_in_traceable_pending_state(self):
        output = io.StringIO()
        file_info = {
            "name": "files/test-media",
            "uri": "https://generativelanguage.googleapis.com/file/test-media",
        }
        with mock.patch.object(
                 gemini_reverse,
                 "_open",
                 side_effect=RuntimeError("secret cleanup detail"),
             ) as opened, \
             mock.patch.object(gemini_reverse.time, "sleep") as sleep, \
             mock.patch("sys.stdout", output):
            result = gemini_reverse._delete_file(file_info, "test-key")
        self.assertEqual(result, {
            "status": "pending_provider_cleanup",
            "attempts": 3,
            "persisted": False,
        })
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            list(gemini_reverse.CLEANUP_RETRY_DELAYS_SECONDS),
        )
        logged = output.getvalue()
        self.assertIn('"cleanup_pending": true', logged)
        self.assertIn('"status": "pending_provider_cleanup"', logged)
        self.assertIn("resource_sha256", logged)
        self.assertNotIn("files/test-media", logged)
        self.assertNotIn("test-key", logged)
        self.assertNotIn("secret cleanup detail", logged)

    def _cleanup_db(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = pathlib.Path(directory.name) / "content_jobs.db"

        def connect():
            connection = sqlite3.connect(str(path))
            connection.row_factory = sqlite3.Row
            return connection

        return connect

    def test_cleanup_exhaustion_persists_and_worker_recovery_removes_record(self):
        jdb = self._cleanup_db()
        file_info = {
            "name": "files/test-media",
            "uri": "https://generativelanguage.googleapis.com/file/test-media",
        }
        with mock.patch.object(
                 gemini_reverse,
                 "_delete_resource",
                 side_effect=RuntimeError("provider unavailable"),
             ) as immediate_delete, \
             mock.patch.object(gemini_reverse.time, "sleep"):
            result = gemini_reverse._delete_file(
                file_info,
                "test-key",
                cleanup_jdb=jdb,
            )
        self.assertEqual(immediate_delete.call_count, 3)
        self.assertTrue(result["persisted"])
        with closing(jdb()) as connection:
            row = connection.execute(
                "SELECT resource_name,created_at,attempts,next_retry_at," \
                "expires_at,status FROM gemini_file_cleanup_outbox"
            ).fetchone()
        self.assertEqual(row["resource_name"], "files/test-media")
        self.assertGreater(row["created_at"], 0)
        self.assertEqual(row["attempts"], 3)
        self.assertGreater(row["next_retry_at"], row["created_at"])
        self.assertEqual(row["status"], "pending")
        self.assertLessEqual(
            row["expires_at"] - row["created_at"],
            gemini_reverse.CLEANUP_QUEUE_RETENTION_SECONDS,
        )

        # Simulate a later worker/process recovery after the durable retry time.
        with mock.patch.object(gemini_reverse, "_delete_resource") as recovered:
            drained = gemini_reverse.drain_cleanup_once(
                jdb,
                api_key="test-key",
                now=row["next_retry_at"],
            )
        self.assertTrue(drained)
        recovered.assert_called_once_with("files/test-media", "test-key")
        with closing(jdb()) as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM gemini_file_cleanup_outbox"
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_cleanup_retry_boundary_removes_record_and_logs_final_state(self):
        jdb = self._cleanup_db()
        gemini_reverse._persist_cleanup(
            jdb,
            "files/test-media",
            gemini_reverse.CLEANUP_QUEUE_MAX_ATTEMPTS,
            now=100,
        )
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            claimed = gemini_reverse._claim_cleanup(jdb, now=131)
        self.assertIsNone(claimed)
        with closing(jdb()) as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM gemini_file_cleanup_outbox"
            ).fetchone()[0]
        self.assertEqual(remaining, 0)
        self.assertIn("retry_window_exhausted", output.getvalue())
        self.assertNotIn("files/test-media", output.getvalue())

    def test_cleanup_worker_is_registered_as_daemon_startup_recovery(self):
        jdb = self._cleanup_db()
        previous = gemini_reverse._cleanup_worker_started
        self.addCleanup(
            setattr,
            gemini_reverse,
            "_cleanup_worker_started",
            previous,
        )
        gemini_reverse._cleanup_worker_started = False
        thread = mock.MagicMock()
        with mock.patch.object(
                 gemini_reverse.threading,
                 "Thread",
                 return_value=thread,
             ) as thread_factory:
            self.assertTrue(gemini_reverse.start_cleanup_worker(jdb))
            self.assertFalse(gemini_reverse.start_cleanup_worker(jdb))
        thread.start.assert_called_once_with()
        call = thread_factory.call_args
        self.assertIs(call.kwargs["target"], gemini_reverse.cleanup_scanner)
        self.assertEqual(call.kwargs["args"], (jdb,))
        self.assertEqual(call.kwargs["name"], "gemini-file-cleanup-recover")
        self.assertTrue(call.kwargs["daemon"])
        core_source = pathlib.Path(
            breakdown.__file__
        ).with_name("core.py").read_text(encoding="utf-8")
        self.assertIn("gemini_reverse.start_cleanup_worker(jdb)", core_source)

    def test_lost_delete_response_then_recovery_404_completes_cleanup(self):
        jdb = self._cleanup_db()
        file_info = {
            "name": "files/test-media",
            "uri": "https://generativelanguage.googleapis.com/file/test-media",
        }
        with mock.patch.object(
                 gemini_reverse,
                 "_delete_resource",
                 side_effect=RuntimeError("response lost"),
             ), \
             mock.patch.object(gemini_reverse.time, "sleep"):
            result = gemini_reverse._delete_file(
                file_info,
                "test-key",
                cleanup_jdb=jdb,
            )
        self.assertTrue(result["persisted"])
        with closing(jdb()) as connection:
            retry_at = connection.execute(
                "SELECT next_retry_at FROM gemini_file_cleanup_outbox"
            ).fetchone()[0]

        not_found = self._http_error(404, {
            "error": {
                "code": 404,
                "status": "NOT_FOUND",
                "message": "file no longer exists",
            },
        })
        output = io.StringIO()
        with mock.patch.object(
                 urllib.request,
                 "urlopen",
                 side_effect=not_found,
             ) as opened, \
             mock.patch("sys.stdout", output):
            drained = gemini_reverse.drain_cleanup_once(
                jdb,
                api_key="test-key",
                now=retry_at,
            )
        self.assertTrue(drained)
        self.assertEqual(opened.call_count, 1)
        with closing(jdb()) as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM gemini_file_cleanup_outbox"
            ).fetchone()[0]
        self.assertEqual(remaining, 0)
        self.assertIn("already_absent_by_recovery", output.getvalue())
        self.assertIn('"cleanup_pending": false', output.getvalue())
        self.assertNotIn("files/test-media", output.getvalue())

    def test_recovery_403_keeps_cleanup_pending(self):
        jdb = self._cleanup_db()
        gemini_reverse._persist_cleanup(
            jdb,
            "files/test-media",
            attempts=3,
            now=100,
        )
        forbidden = self._http_error(403, {
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "message": "forbidden",
            },
        })
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=forbidden,
        ):
            self.assertTrue(gemini_reverse.drain_cleanup_once(
                jdb,
                api_key="test-key",
                now=130,
            ))
        with closing(jdb()) as connection:
            row = connection.execute(
                "SELECT status,attempts,next_retry_at " \
                "FROM gemini_file_cleanup_outbox"
            ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 4)
        self.assertGreater(row["next_retry_at"], 130)


if __name__ == "__main__":
    unittest.main()
