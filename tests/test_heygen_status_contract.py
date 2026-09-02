# -*- coding: utf-8 -*-
"""Adversarial tests for paid HeyGen task status reconciliation."""
import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

video = importlib.import_module("content_domains.video")


class HeyGenStatusShapeTests(unittest.TestCase):
    def test_nested_state_alias_and_url_are_normalized(self):
        info, status, recognized = video._heygen_video_info({
            "result": {"video": {"state": "done", "url": "https://cdn.example/video.mp4"}}
        })
        self.assertTrue(recognized)
        self.assertEqual(status, "completed")
        self.assertEqual(info["video_url"], "https://cdn.example/video.mp4")

    def test_media_alias_may_come_from_selected_status_ancestor(self):
        info, status, recognized = video._heygen_video_info({
            "result": {
                "video_url": "https://cdn.example/video.mp4",
                "video": {"state": "done"},
            }
        })
        self.assertTrue(recognized)
        self.assertEqual(status, "completed")
        self.assertEqual(info["video_url"], "https://cdn.example/video.mp4")

    def test_sibling_wrapper_media_is_ambiguous_instead_of_joined(self):
        info, status, recognized = video._heygen_video_info({
            "data": {"status": "completed"},
            "result": {"video_url": "https://cdn.example/unrelated.mp4"},
        })
        self.assertFalse(recognized)
        self.assertEqual(status, "completed")
        self.assertNotIn("video_url", info)

    def test_sibling_wrapper_status_is_ambiguous(self):
        _, _, recognized = video._heygen_video_info({
            "data": {"status": "completed", "video_url": "https://cdn.example/a.mp4"},
            "result": {"status": "processing"},
        })
        self.assertFalse(recognized)

    def test_conflicting_statuses_on_one_wrapper_path_are_ambiguous(self):
        _, _, recognized = video._heygen_video_info({
            "result": {
                "status": "processing",
                "video": {"status": "completed", "video_url": "https://cdn.example/a.mp4"},
            }
        })
        self.assertFalse(recognized)

    def test_conflicting_status_and_state_on_same_node_are_ambiguous(self):
        for status_value, state_value in (
            ("completed", "failed"),
            ("failed", "completed"),
            ("processing", "completed"),
        ):
            with self.subTest(status=status_value, state=state_value):
                info, _, recognized = video._heygen_video_info({
                    "data": {
                        "status": status_value,
                        "state": state_value,
                        "video_url": "https://cdn.example/a.mp4",
                    }
                })
                self.assertFalse(recognized)
                self.assertEqual(info["status"], status_value)

    def test_equivalent_status_and_state_aliases_on_same_node_are_accepted(self):
        info, status, recognized = video._heygen_video_info({
            "data": {
                "status": "done",
                "state": "completed",
                "video_url": "https://cdn.example/a.mp4",
            }
        })
        self.assertTrue(recognized)
        self.assertEqual(status, "completed")
        self.assertEqual(info["status"], "completed")
        self.assertEqual(info["video_url"], "https://cdn.example/a.mp4")

    def test_unknown_status_is_not_treated_as_processing(self):
        _, status, recognized = video._heygen_video_info({"data": {"status": "mystery"}})
        self.assertFalse(recognized)
        self.assertEqual(status, "mystery")

    def test_submitted_is_a_supported_active_state(self):
        info, status, recognized = video._heygen_video_info({"status": "submitted"})
        self.assertTrue(recognized)
        self.assertEqual(status, "pending")
        self.assertEqual(info["status"], "pending")

    def test_safe_shape_never_contains_response_values(self):
        payload = {
            "data": {"status": "", "video_url": "https://secret.example/?token=do-not-log"},
            "api_key": "do-not-log-key",
        }
        diagnostic = video._heygen_status_shape(payload)
        self.assertNotIn("do-not-log", diagnostic)
        self.assertNotIn("secret.example", diagnostic)
        self.assertEqual(diagnostic, "top_type=dict known_keys=data data_type=dict")

    def test_arbitrary_nested_status_is_not_trusted(self):
        _, status, recognized = video._heygen_video_info({
            "meta": {"status": "completed", "video_url": "https://attacker.example/video.mp4"}
        })
        self.assertFalse(recognized)
        self.assertEqual(status, "")

    def test_v1_crosscheck_encodes_provider_id_and_is_get_only(self):
        with mock.patch.object(video, "_HEYGEN_DIRECT_API", "https://api.heygen.test"), \
             mock.patch.object(video, "_heygen_direct_req", return_value={"data": {}}) as request:
            video._heygen_video_status_v1("task/id?token=not-a-query")
        request.assert_called_once_with(
            "GET",
            "https://api.heygen.test/v1/video_status.get?video_id=task%2Fid%3Ftoken%3Dnot-a-query",
            ctype=None,
            timeout=30,
        )


class HeyGenStatusPollingTests(unittest.TestCase):
    def _poll(self, v3_responses, v1_response=None):
        with mock.patch.object(video, "HEYGEN_POLL_INTERVAL", 0), \
             mock.patch.object(video, "HEYGEN_STATUS_CROSSCHECK_AFTER", 3), \
             mock.patch.object(video, "_heygen_request_json", side_effect=v3_responses) as request, \
             mock.patch.object(video, "_heygen_video_status_v1", return_value=v1_response) as crosscheck:
            result = video._heygen_poll_video("provider-task-id", direct=True, deadline_s=60)
        return result, request, crosscheck

    def test_three_empty_v3_responses_recover_from_v1_completed(self):
        result, request, crosscheck = self._poll(
            [{"data": {}}, {"data": {}}, {"data": {}}],
            {"data": {"status": "completed", "video_url": "https://cdn.example/video.mp4"}},
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(request.call_count, 3)
        crosscheck.assert_called_once_with("provider-task-id")

    def test_failed_v1_crosscheck_does_not_resubmit_or_abort_eventual_v3_success(self):
        v3 = [
            {"data": {}}, {"data": {}}, {"data": {}},
            {"data": {"status": "processing"}},
            {"data": {"status": "completed", "video_url": "https://cdn.example/video.mp4"}},
        ]
        with mock.patch.object(video, "HEYGEN_POLL_INTERVAL", 0), \
             mock.patch.object(video, "HEYGEN_STATUS_CROSSCHECK_AFTER", 3), \
             mock.patch.object(video, "_heygen_request_json", side_effect=v3) as request, \
             mock.patch.object(video, "_heygen_video_status_v1", side_effect=RuntimeError("404 body secret")) as crosscheck, \
             mock.patch("builtins.print") as output:
            result = video._heygen_poll_video("provider-task-id", direct=True, deadline_s=60)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(request.call_count, 5)
        crosscheck.assert_called_once_with("provider-task-id")
        logs = " ".join(str(call) for call in output.call_args_list)
        self.assertNotIn("404 body secret", logs)

    def test_mcp_get_error_fallback_recovers_without_logging_response_values(self):
        secret_error = RuntimeError(
            "provider secret https://files.heygen.ai/x?Signature=LEAK"
        )
        completed = {
            "data": {
                "status": "completed",
                "video_url": "https://cdn.example/right.mp4",
            }
        }
        with mock.patch.object(video, "_heygen_mcp_call", side_effect=secret_error) as mcp_call, \
             mock.patch.object(video, "_heygen_request_json", return_value=completed) as request, \
             mock.patch("builtins.print") as output:
            result = video._heygen_poll_video(
                "provider-task-id", direct=True, deadline_s=60, mcp=True,
            )
        self.assertEqual(result["video_url"], "https://cdn.example/right.mp4")
        mcp_call.assert_called_once_with(
            "get_video", {"videoId": "provider-task-id"}, timeout=90,
        )
        request.assert_called_once_with(
            "GET", "/videos/provider-task-id", timeout=90, direct=True,
        )
        logs = " ".join(str(call) for call in output.call_args_list)
        for secret in (
            "provider secret", "Signature=LEAK", "files.heygen.ai", "/x?",
        ):
            self.assertNotIn(secret, logs)
        self.assertIn("error_type=RuntimeError", logs)

    def test_failed_status_never_logs_or_raises_provider_secrets(self):
        payload = {
            "data": {
                "status": "failed",
                "video_url": "https://files2.heygen.ai/private.mp4?Signature=secret-token",
                "error": "provider-secret-value",
                "failure_code": "secret-code",
            }
        }
        with mock.patch.object(video, "HEYGEN_POLL_INTERVAL", 0), \
             mock.patch.object(video, "_heygen_request_json", return_value=payload), \
             mock.patch("builtins.print") as output:
            with self.assertRaisesRegex(RuntimeError, "上游返回失败状态") as error:
                video._heygen_poll_video("provider-task-id", direct=True, deadline_s=60)
        logs = " ".join(str(call) for call in output.call_args_list)
        exposed = logs + " " + str(error.exception)
        for secret in (
            "secret-token", "provider-secret-value", "secret-code",
            "files2.heygen.ai", "private.mp4", "Signature=",
        ):
            self.assertNotIn(secret, exposed)
        self.assertIn("failure_kind=provider", logs)

    def test_ambiguous_sibling_media_keeps_polling_until_coherent_success(self):
        responses = [
            {
                "data": {"status": "completed"},
                "result": {"video_url": "https://cdn.example/unrelated.mp4"},
            },
            {"data": {"status": "completed", "video_url": "https://cdn.example/right.mp4"}},
        ]
        with mock.patch.object(video, "HEYGEN_POLL_INTERVAL", 0), \
             mock.patch.object(video, "_heygen_request_json", side_effect=responses) as request, \
             mock.patch.object(video, "_heygen_video_status_v1") as crosscheck:
            result = video._heygen_poll_video("provider-task-id", direct=True, deadline_s=60)
        self.assertEqual(result["video_url"], "https://cdn.example/right.mp4")
        self.assertEqual(request.call_count, 2)
        crosscheck.assert_not_called()

    def test_moderation_failure_keeps_safe_actionable_message(self):
        payload = {"data": {"status": "failed", "error": "real person secret detail"}}
        with mock.patch.object(video, "HEYGEN_POLL_INTERVAL", 0), \
             mock.patch.object(video, "_heygen_request_json", return_value=payload), \
             mock.patch("builtins.print") as output:
            with self.assertRaisesRegex(RuntimeError, "内容审核未通过") as error:
                video._heygen_poll_video("provider-task-id", direct=True, deadline_s=60)
        exposed = " ".join(str(call) for call in output.call_args_list) + str(error.exception)
        self.assertNotIn("secret detail", exposed)
        self.assertIn("failure_kind=moderation", exposed)

    def test_never_visible_task_has_precise_error_instead_of_false_timeout(self):
        with mock.patch.object(video.time, "time", side_effect=[0, 0, 2]), \
             mock.patch.object(video.time, "sleep"), \
             mock.patch.object(video, "_heygen_request_json", return_value={"data": {}}), \
             mock.patch.object(video, "_heygen_video_status_v1") as crosscheck:
            with self.assertRaises(video.HeyGenStatusUnknownError) as error:
                video._heygen_poll_video("provider-task-id", direct=True, deadline_s=1)
        self.assertIn("禁止重复提交", str(error.exception))
        crosscheck.assert_not_called()

    def test_recognized_processing_task_keeps_ordinary_timeout_semantics(self):
        with mock.patch.object(video.time, "time", side_effect=[0, 0, 2]), \
             mock.patch.object(video.time, "sleep"), \
             mock.patch.object(video, "_heygen_request_json", return_value={"data": {"status": "processing"}}), \
             mock.patch.object(video, "_heygen_video_status_v1") as crosscheck:
            with self.assertRaisesRegex(TimeoutError, "HeyGen视频生成超时"):
                video._heygen_poll_video("provider-task-id", direct=True, deadline_s=1)
        crosscheck.assert_not_called()

    def test_paid_status_failure_never_falls_back_to_a_second_submission(self):
        paid_error = video.HeyGenBilledError("status contract unknown")
        with mock.patch.object(video, "_HEYGEN_DIRECT", True), \
             mock.patch.object(video, "HEYGEN_API_KEY", "configured"), \
             mock.patch.object(video, "generate_heygen_video_direct", side_effect=paid_error) as direct, \
             mock.patch.object(video, "_resolve_out_file") as relay_fallback:
            with self.assertRaises(video.HeyGenBilledError):
                video.generate_heygen_video(
                    "image.jpg", "audio.mp3", "720p", "9:16", "normal", job_id=7471,
                )
        direct.assert_called_once()
        relay_fallback.assert_not_called()

    def test_programming_error_in_crosscheck_is_not_hidden_as_provider_timeout(self):
        with mock.patch.object(video, "HEYGEN_POLL_INTERVAL", 0), \
             mock.patch.object(video, "HEYGEN_STATUS_CROSSCHECK_AFTER", 3), \
             mock.patch.object(video, "_heygen_request_json", return_value={"data": {}}), \
             mock.patch.object(video, "_heygen_video_status_v1", side_effect=TypeError("bug")):
            with self.assertRaisesRegex(TypeError, "bug"):
                video._heygen_poll_video("provider-task-id", direct=True, deadline_s=60)

    def test_sustained_network_failure_is_not_mislabeled_as_status_contract_error(self):
        with mock.patch.object(video.time, "time", side_effect=[0, 0, 2]), \
             mock.patch.object(video.time, "sleep"), \
             mock.patch.object(
                 video, "_heygen_request_json",
                 side_effect=video.HeyGenNetworkError("temporary network failure"),
             ), \
             mock.patch.object(video, "_heygen_video_status_v1") as crosscheck:
            with self.assertRaisesRegex(video.HeyGenNetworkError, "网络持续失败"):
                video._heygen_poll_video("provider-task-id", direct=True, deadline_s=1)
        crosscheck.assert_not_called()


if __name__ == "__main__":
    unittest.main()
