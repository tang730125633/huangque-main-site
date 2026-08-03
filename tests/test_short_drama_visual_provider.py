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


class ShortDramaVisualProviderTests(unittest.TestCase):
    def test_no_provider_is_explicitly_unavailable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            snapshot = capability_snapshot()
            provider = load_from_environment()
        self.assertEqual("provider_not_selected", snapshot["code"])
        self.assertFalse(snapshot["configured"])
        self.assertIsNone(provider)

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
