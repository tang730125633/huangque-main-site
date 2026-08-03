import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.content_domains import short_drama_sound_effect
from server.providers.sound_effects import (
    ProviderConfigurationError,
    configured_provider,
)


class ShortDramaSoundEffectProviderTests(unittest.TestCase):
    def test_mock_provider_is_test_only(self):
        with self.assertRaises(ProviderConfigurationError):
            configured_provider({
                "SOUND_EFFECT_PROVIDER": "mock",
                "SOUND_EFFECT_ALLOW_MOCK": "0",
            })

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg and ffprobe are required",
    )
    def test_worker_normalizes_mock_audio_and_preserves_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def out_path(relative):
                return root / relative

            with mock.patch.dict(os.environ, {
                "SOUND_EFFECT_PROVIDER": "mock",
                "SOUND_EFFECT_ALLOW_MOCK": "1",
            }, clear=False), mock.patch.object(
                short_drama_sound_effect, "_out_path", out_path
            ), mock.patch.object(
                short_drama_sound_effect, "public_url",
                lambda relative, _content_type: "/file/" + relative,
            ):
                result = (
                    short_drama_sound_effect.gen_short_drama_sound_effect({
                        "prompt": "Soft rain ambience without speech or music",
                        "duration_seconds": 0.6,
                        "loop": True,
                        "suggestion_id": "suggestion-1",
                        "project_id": "project-1",
                        "owner_username": "owner",
                        "shot_id": "shot-1",
                        "kind": "ambience",
                        "_username": "editor",
                        "_job_id": 101,
                    })
                )
            self.assertEqual("sound_effect", result["asset_kind"])
            self.assertEqual("owner", result["sound_design"]["owner_username"])
            self.assertEqual("passed", result["quality"]["decision"])
            self.assertEqual(48000, result["quality"]["sample_rate"])
            self.assertEqual(2, result["quality"]["channels"])
            self.assertTrue((root / result["file"]).is_file())
            self.assertFalse(any(root.rglob("*_source.*")))


if __name__ == "__main__":
    unittest.main()
