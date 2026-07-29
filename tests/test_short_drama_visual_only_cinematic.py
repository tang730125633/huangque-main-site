# -*- coding: utf-8 -*-
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

os.environ.setdefault("CONTENT_BASE", tempfile.mkdtemp())
video = importlib.import_module("content_domains.video")


class VisualOnlyCinematicTests(unittest.TestCase):
    def test_uses_creatable_silent_target_and_returns_user_prompt(self):
        provider_prompt = (
            "海边跳舞\n\n[VISUAL-ONLY PRODUCTION CONSTRAINTS]\n"
            "- Generate picture only."
        )
        source_path = video._out_path("video/out.mp4")
        sanitized = {
            "source_report": {
                "video": {"codec": "h264"},
                "audio": {"codec": "aac"},
            },
            "silent_report": {
                "video": {"codec": "h264"},
                "audio": None,
            },
        }
        with patch.object(
            video,
            "get_video_avatar",
            return_value={"provider_avatar_id": "look1", "name": "actor"},
        ), patch.object(
            video, "update_video_asset_phase"
        ), patch.object(
            video, "heygen_slot"
        ), patch.object(
            video,
            "_heygen_retry_429",
            side_effect=lambda fn, what="": fn(),
        ), patch.object(
            video, "_heygen_create_cinematic_video", return_value="vid1"
        ) as create, patch.object(
            video,
            "_heygen_poll_video",
            return_value={"video_url": "https://x/y.mp4", "duration": 10},
        ), patch.object(
            video,
            "_download_video_file_direct",
            return_value="video/out.mp4",
        ), patch.object(
            video, "_resolve_out_file", return_value=source_path
        ), patch.object(
            video.short_drama_media_sanitize,
            "sanitize_visual_source",
            return_value=sanitized,
        ) as sanitize, patch.object(
            video, "_extract_first_frame_cover", return_value=None
        ), patch.object(
            video,
            "public_url",
            return_value="https://cos.example/video/out_silent.mp4",
        ):
            result = video.gen_cinematic({
                "_username": "kongli",
                "_job_id": 1,
                "_short_drama_video": {
                    "visual_only": True,
                    "user_prompt": "海边跳舞",
                    "prompt_template_version": "visual-v1",
                    "compiled_prompt_hash": "a" * 64,
                },
                "avatar_ids": [1],
                "prompt": provider_prompt,
                "resolution": "720p",
                "ratio": "9:16",
                "duration": 10,
            })

        sanitize.assert_called_once()
        source, destination = sanitize.call_args.args
        self.assertEqual(source_path, source)
        self.assertIsNotNone(destination)
        self.assertEqual("out_silent.mp4", destination.name)
        self.assertEqual("video", destination.parent.name)
        self.assertEqual("video/out_silent.mp4", result["video_file"])
        self.assertEqual("海边跳舞", result["text"])
        self.assertEqual("海边跳舞", result["prompt"])
        self.assertNotIn("VISUAL-ONLY", result["text"])
        self.assertEqual(
            provider_prompt,
            create.call_args.kwargs["prompt"],
        )

    def test_missing_user_prompt_is_rejected_before_provider_call(self):
        with patch.object(video, "_heygen_create_cinematic_video") as create:
            with self.assertRaisesRegex(ValueError, "原始用户提示词"):
                video.gen_cinematic({
                    "_username": "kongli",
                    "_job_id": 1,
                    "_short_drama_video": {"visual_only": True},
                    "avatar_ids": [1],
                    "prompt": "compiled internal prompt",
                    "resolution": "720p",
                    "ratio": "9:16",
                    "duration": 10,
                })
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
