import os
import sys
import unittest
from pathlib import Path
from unittest import mock


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from providers.short_drama_visual import capability_snapshot, load_from_environment
from providers.short_drama_visual.base import VisualProviderError
from providers.short_drama_visual.heygen_cinematic import (
    HeyGenCinematicShotProvider,
)
from providers.short_drama_visual.grok_xai import GrokXaiShotProvider
from providers.short_drama_visual import minimax_h3
from providers.short_drama_visual.minimax_h3 import MiniMaxH3ShotProvider
from content_domains import provider_keys, video_minimax_h3


class ShortDramaVisualProviderTests(unittest.TestCase):
    def test_minimax_h3_is_the_default_short_drama_provider(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            snapshot = capability_snapshot()
            provider = load_from_environment()
        self.assertEqual("provider_not_configured", snapshot["code"])
        self.assertFalse(snapshot["configured"])
        self.assertEqual("minimax_h3", snapshot["selected"])
        self.assertIsInstance(provider, MiniMaxH3ShotProvider)

    def test_minimax_h3_normalizes_up_to_five_character_references(self):
        provider = MiniMaxH3ShotProvider()
        with mock.patch.object(
            provider, "_reference_value", return_value="data:image/png;base64,AA=="
        ):
            result = provider.validate_request({
                "prompt": "两个孩子在长椅上分享糖果",
                "ratio": "16:9",
                "resolution": "768p",
                "duration_seconds": 5,
                "reference_images": [
                    {"character_key": "boy", "file": "image/boy.png"},
                    {"character_key": "girl", "url": "https://cdn.example/girl.png"},
                ],
            })
        self.assertEqual("minimax_h3", result["provider"])
        self.assertEqual("MiniMax-H3", result["model"])
        self.assertEqual(5, result["duration_seconds"])
        self.assertEqual("768p", result["resolution"])
        self.assertEqual(2, len(result["reference_images"]))

    def test_minimax_h3_encodes_local_png_without_public_storage(self):
        provider = MiniMaxH3ShotProvider()
        image_path = mock.Mock()
        image_path.is_file.return_value = True
        image_path.stat.return_value.st_size = 12
        image_path.read_bytes.return_value = b"\x89PNG\r\n\x1a\nTEST"
        with mock.patch("content_domains.core._out_path", return_value=image_path):
            value = provider._reference_value({"file": "image/role.png"})
        self.assertTrue(value.startswith("data:image/png;base64,"))

    def test_minimax_h3_preflight_rejects_missing_local_reference(self):
        provider = MiniMaxH3ShotProvider()
        image_path = mock.Mock()
        image_path.is_file.return_value = False
        with mock.patch("content_domains.core._out_path", return_value=image_path):
            with self.assertRaises(VisualProviderError) as raised:
                provider.validate_request({
                    "prompt": "two characters share candy",
                    "ratio": "16:9",
                    "duration_seconds": 5,
                    "reference_images": [{"file": "image/missing.png"}],
                })
        self.assertEqual("visual_reference_unavailable", raised.exception.code)

    def test_minimax_h3_create_poll_and_fetch_preserve_key_affinity(self):
        provider = MiniMaxH3ShotProvider()
        request = {
            "prompt": "两个孩子在长椅上分享糖果",
            "ratio": "16:9",
            "duration_seconds": 5,
            "reference_images": [
                {"url": "https://cdn.example/boy.png"},
                {"url": "https://cdn.example/girl.png"},
            ],
        }
        candidate = {"id": "minimax-key-2", "secret": "test-only-secret"}
        with mock.patch.object(provider_keys, "has_candidate", return_value=True), \
             mock.patch.object(provider, "_claim_key", return_value=candidate), \
             mock.patch.object(provider, "_bound_key", return_value=candidate), \
             mock.patch("content_domains.video_minimax_h3._request_json", side_effect=[
                 {"task_id": "task-8"},
                 {"task": {"status": "succeeded", "content": {"url": "https://cdn.example/result.mp4"}}},
             ]) as request_json, \
             mock.patch("content_domains.video._download_video_file_direct", return_value="video/minimax-result.mp4") as download:
            created = provider.create_job(request)
            state = provider.get_job(created["provider_job_id"])
            result = provider.fetch_result(created["provider_job_id"], state["result_url"])
        self.assertEqual("succeeded", state["status"])
        self.assertEqual("video/minimax-result.mp4", result["file"])
        submitted = request_json.call_args_list[0].args[3]
        self.assertEqual("MiniMax-H3", submitted["model"])
        self.assertTrue(submitted["content"][1]["image_url"]["url"].startswith("https://"))
        self.assertEqual("/v2/video_generation", request_json.call_args_list[0].args[2])
        self.assertEqual("/v2/query/video_generation/task-8", request_json.call_args_list[1].args[2])
        self.assertEqual("test-only-secret", request_json.call_args_list[0].kwargs["api_key"])
        self.assertEqual("test-only-secret", request_json.call_args_list[1].kwargs["api_key"])
        self.assertEqual(
            set(minimax_h3.MINIMAX_RESULT_HOSTS),
            set(download.call_args.kwargs["allowed_hosts"]),
        )
        self.assertEqual(
            minimax_h3.MINIMAX_RESULT_MAX_BYTES,
            download.call_args.kwargs["max_bytes"],
        )

    def test_minimax_h3_failed_job_exposes_safe_provider_reason(self):
        provider = MiniMaxH3ShotProvider()
        candidate = {"id": "minimax-key-2", "secret": "test-only-secret"}
        provider_job_id = provider._encode_job_id(candidate["id"], "task-failed")
        with mock.patch.object(provider, "_bound_key", return_value=candidate), \
             mock.patch("content_domains.video_minimax_h3.query_task", return_value={
                 "task": {
                     "status": "failed",
                     "error": {
                         "code": "content_risk",
                         "message": "reference image did not pass review",
                     },
                 },
             }):
            state = provider.get_job(provider_job_id)
        self.assertEqual("failed", state["status"])
        self.assertEqual("content_risk", state["failure"]["code"])
        self.assertEqual(
            "reference image did not pass review", state["failure"]["message"]
        )

    def test_minimax_invalid_key_is_blocked_before_charge(self):
        provider = MiniMaxH3ShotProvider()
        candidate = {"id": "bad-minimax-key", "secret": "bad-secret"}
        with mock.patch.object(provider, "_claim_key", return_value=candidate), \
             mock.patch(
                 "content_domains.video_minimax_h3.check_credentials",
                 side_effect=video_minimax_h3.MiniMaxCredentialRejected(
                     "MiniMax 开放平台密钥无效"
                 ),
             ), mock.patch.object(provider_keys, "set_health") as set_health:
            with self.assertRaises(VisualProviderError) as raised:
                provider.prepare_job({"provider": "minimax_h3"})
        self.assertEqual("provider_not_configured", raised.exception.code)
        set_health.assert_called_once_with(
            "bad-minimax-key", False, error="MiniMax 开放平台密钥无效"
        )

    def test_selected_provider_without_key_is_not_ready(self):
        with mock.patch.dict(
            os.environ,
            {"HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "heygen_cinematic"},
            clear=True,
        ):
            snapshot = capability_snapshot()
            provider = load_from_environment()
        self.assertEqual("provider_not_configured", snapshot["code"])
        self.assertFalse(snapshot["configured"])
        self.assertIsInstance(provider, HeyGenCinematicShotProvider)

    def test_grok_provider_is_selected_with_xai_key(self):
        with mock.patch.dict(
            os.environ,
            {
                "HQ_SHORT_DRAMA_AUTODRAFT_PROVIDER": "grok",
                "XAI_API_KEY": "configured-for-test",
            },
            clear=True,
        ), mock.patch.object(provider_keys, "has_candidate", return_value=True):
            snapshot = capability_snapshot()
            provider = load_from_environment()
        self.assertEqual("provider_ready", snapshot["code"])
        self.assertEqual("grok", snapshot["selected"])
        self.assertIsInstance(provider, GrokXaiShotProvider)

    def test_grok_request_uses_character_reference_image(self):
        result = GrokXaiShotProvider().validate_request({
            "prompt": "雨夜里，记者推开档案室的门",
            "ratio": "16:9",
            "resolution": "720P",
            "duration_seconds": 5,
            "reference_image_file": "avatar/reference.png",
        })
        self.assertEqual("grok", result["provider"])
        self.assertEqual("avatar/reference.png", result["reference_image_file"])
        self.assertEqual("720p", result["resolution"])

    def test_grok_create_poll_and_fetch_preserve_key_affinity(self):
        provider = GrokXaiShotProvider()
        request = {
            "prompt": "雨夜里，记者推开档案室的门",
            "ratio": "16:9",
            "resolution": "720p",
            "duration_seconds": 5,
            "reference_image_url": "https://cdn.example/avatar.png",
        }
        candidate = {"id": "key-7", "secret": "test-only-secret"}
        with mock.patch.dict(os.environ, {"XAI_API_KEY": "configured"}, clear=True), \
             mock.patch.object(provider_keys, "has_candidate", return_value=True), \
             mock.patch.object(provider, "_claim_key", return_value=candidate), \
             mock.patch.object(provider, "_bound_key", return_value=candidate), \
             mock.patch("content_domains.video_xai._create", return_value={"request_id": "req-9"}) as create, \
             mock.patch("content_domains.video_xai._request_json", return_value={
                 "status": "done", "video": {"url": "https://cdn.example/result.mp4"}
             }) as poll, \
             mock.patch("content_domains.video._download_video_file_direct", return_value="video/grok-result.mp4"):
            created = provider.create_job(request)
            state = provider.get_job(created["provider_job_id"])
            result = provider.fetch_result(created["provider_job_id"], state["result_url"])
        self.assertNotEqual("req-9", created["provider_job_id"])
        self.assertEqual("succeeded", state["status"])
        self.assertEqual("video/grok-result.mp4", result["file"])
        self.assertEqual("test-only-secret", create.call_args.kwargs["api_key"])
        self.assertEqual("test-only-secret", poll.call_args.kwargs["api_key"])
        self.assertEqual(
            [{"url": "https://cdn.example/avatar.png"}],
            create.call_args.args[2]["reference_images"],
        )

    def test_grok_vault_failure_never_falls_back_to_rotated_environment_key(self):
        provider = GrokXaiShotProvider()
        with mock.patch.dict(os.environ, {"XAI_API_KEY": "rotated-environment-key"}, clear=True), \
             mock.patch.object(
                 provider_keys,
                 "claim_candidate",
                 side_effect=provider_keys.KeyStoreUnavailable(
                     "视频密钥保险箱未配置，已停止新付费任务"
                 ),
             ), \
             mock.patch("content_domains.video_xai._create") as create:
            with self.assertRaises(provider_keys.KeyStoreUnavailable):
                provider._claim_key()
        create.assert_not_called()

    def test_grok_legacy_env_job_resolves_encrypted_snapshot_after_rotation(self):
        provider = GrokXaiShotProvider()
        provider_job_id = provider._encode_job_id("env", "req-legacy")
        snapshot = {"id": "vault-key-a", "secret": "original-key-a"}
        with mock.patch.dict(os.environ, {"XAI_API_KEY": "rotated-key-b"}, clear=True), \
             mock.patch.object(provider_keys, "candidates", return_value=[snapshot]) as candidates, \
             mock.patch("content_domains.video_xai._request_json", return_value={"status": "pending"}) as poll:
            provider.get_job(provider_job_id)
        candidates.assert_called_once_with("xai", preferred_id="env")
        self.assertEqual("original-key-a", poll.call_args.kwargs["api_key"])

    def test_grok_running_job_uses_retired_snapshot_without_active_candidate(self):
        provider = GrokXaiShotProvider()
        provider_job_id = provider._encode_job_id("retired-key-a", "req-retired")
        snapshot = {"id": "retired-key-a", "secret": "retired-secret-a"}
        with mock.patch.object(provider_keys, "has_candidate", return_value=False), \
             mock.patch.object(provider_keys, "candidates", return_value=[snapshot]) as candidates, \
             mock.patch("content_domains.video_xai._request_json", return_value={"status": "pending"}) as poll:
            self.assertFalse(provider.configured)
            state = provider.get_job(provider_job_id)
        self.assertEqual("pending", state["status"])
        candidates.assert_called_once_with("xai", preferred_id="retired-key-a")
        self.assertEqual("retired-secret-a", poll.call_args.kwargs["api_key"])

    def test_valid_shot_request_is_normalized_without_network(self):
        result = HeyGenCinematicShotProvider().validate_request({
            "provider_avatar_id": "avatar-1",
            "prompt": "雨夜里，记者推开档案室的门",
            "ratio": "16:9",
            "resolution": "720P",
            "duration_seconds": 5,
        })
        self.assertEqual("avatar-1", result["provider_avatar_id"])
        self.assertEqual("720p", result["resolution"])
        self.assertEqual(5, result["duration_seconds"])

    def test_missing_avatar_blocks_before_provider_submission(self):
        with self.assertRaises(VisualProviderError) as raised:
            HeyGenCinematicShotProvider().validate_request({
                "prompt": "雨夜街道",
                "ratio": "16:9",
                "duration_seconds": 5,
            })
        self.assertEqual("provider_avatar_required", raised.exception.code)
        self.assertFalse(raised.exception.submitted)

    def test_submit_without_key_blocks_before_network(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(VisualProviderError) as raised:
                HeyGenCinematicShotProvider().create_job({
                    "provider_avatar_id": "avatar-1",
                    "prompt": "雨夜街道",
                    "ratio": "16:9",
                    "duration_seconds": 5,
                })
        self.assertEqual("provider_not_configured", raised.exception.code)
        self.assertFalse(raised.exception.submitted)

    def test_create_job_accepts_shared_helper_string_job_id(self):
        with mock.patch.dict(
            os.environ, {"HEYGEN_API_KEY": "configured-for-test"}, clear=True
        ), mock.patch(
            "content_domains.video._heygen_retry_429",
            return_value="provider-video-123",
        ):
            result = HeyGenCinematicShotProvider().create_job({
                "provider_avatar_id": "avatar-1",
                "prompt": "雨夜街道",
                "ratio": "16:9",
                "duration_seconds": 5,
            })
        self.assertEqual("provider-video-123", result["provider_job_id"])
        self.assertEqual(
            {"video_id": "provider-video-123"}, result["raw"]
        )

    def test_create_job_also_accepts_mapping_job_id(self):
        with mock.patch.dict(
            os.environ, {"HEYGEN_API_KEY": "configured-for-test"}, clear=True
        ), mock.patch(
            "content_domains.video._heygen_retry_429",
            return_value={"video_id": "provider-video-456"},
        ):
            result = HeyGenCinematicShotProvider().create_job({
                "provider_avatar_id": "avatar-1",
                "prompt": "雨夜街道",
                "ratio": "16:9",
                "duration_seconds": 5,
            })
        self.assertEqual("provider-video-456", result["provider_job_id"])
        self.assertEqual(
            {"video_id": "provider-video-456"}, result["raw"]
        )

    def test_fetch_result_uses_authenticated_file_route(self):
        with mock.patch(
            "content_domains.video._download_video_file_direct",
            return_value="video/short-drama-shot.mp4",
        ):
            result = HeyGenCinematicShotProvider().fetch_result(
                "provider-video-789", "https://provider.example/result.mp4"
            )
        self.assertEqual("video/short-drama-shot.mp4", result["file"])
        self.assertEqual(
            "/api/gen/file/video/short-drama-shot.mp4", result["url"]
        )

    def test_unsupported_ratio_is_rejected_locally(self):
        with self.assertRaises(VisualProviderError) as raised:
            HeyGenCinematicShotProvider().validate_request({
                "provider_avatar_id": "avatar-1",
                "prompt": "雨夜街道",
                "ratio": "4:3",
                "duration_seconds": 5,
            })
        self.assertEqual("visual_ratio_unsupported", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
