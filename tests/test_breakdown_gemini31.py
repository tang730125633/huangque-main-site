# -*- coding: utf-8 -*-
import io
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.error
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
        deleted.assert_called_once_with(uploaded, "test-key")

    def test_runtime_integration_uses_gemini_and_exposes_only_audit_summary(self):
        gemini_result = {
            "provider": "google",
            "model": gemini_reverse.MODEL,
            "attempts": 1,
            "prompt": "[00:00-00:04] 主体：白色矩形",
            "attempt_audit": [{"attempt": 1, "validation": "passed"}],
            "entries": [{
                "segment_id": 1,
                "start_seconds": 0.0,
                "end_seconds": 4.0,
                "readiness": {"ready": 17, "applicable": 17, "percent": 100.0},
                "facts": {
                    key: {"value": "fact", "evidence_seconds": [0.0]}
                    for key in gemini_reverse.FACT_FIELDS
                },
            }],
        }
        with mock.patch.object(
            gemini_reverse, "analyze_video", return_value=gemini_result
        ) as analyze, mock.patch.object(
            breakdown, "_frame_thumbnails", return_value=[]
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
            urllib.request, "urlopen", side_effect=error
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
            urllib.request,
            "urlopen",
            side_effect=[self._http_error(429), response],
        ) as opened:
            got = gemini_reverse._open(request)
        self.assertIs(got, response)
        self.assertEqual(opened.call_count, 2)

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


if __name__ == "__main__":
    unittest.main()
