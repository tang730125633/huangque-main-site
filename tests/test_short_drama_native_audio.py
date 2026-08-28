import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SERVER_DIR = str(Path(__file__).resolve().parents[1] / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from content_domains import short_drama_native_audio


class ShortDramaNativeAudioTests(unittest.TestCase):
    @staticmethod
    def _probe(audio=None):
        return lambda _path: {
            "duration_ms": 5000,
            "video": {"codec": "h264", "width": 1280, "height": 720},
            "audio": audio,
        }

    @staticmethod
    def _runner(stderr, returncode=0):
        return lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode, stdout="", stderr=stderr,
        )

    def test_rejects_missing_audio_stream(self):
        with self.assertRaises(short_drama_native_audio.NativeAudioError) as raised:
            short_drama_native_audio.inspect_native_audio(
                "shot.mp4", probe=self._probe(audio=None), runner=self._runner("")
            )
        self.assertEqual("provider_audio_missing", raised.exception.code)

    def test_rejects_max_volume_at_silence_threshold(self):
        with self.assertRaises(short_drama_native_audio.NativeAudioError) as raised:
            short_drama_native_audio.inspect_native_audio(
                "shot.mp4",
                probe=self._probe({"codec": "aac", "sample_rate": 48000, "channels": 2}),
                runner=self._runner(
                    "[Parsed_volumedetect_0] mean_volume: -72.0 dB\n"
                    "[Parsed_volumedetect_0] max_volume: -60.0 dB\n"
                ),
            )
        self.assertEqual("provider_audio_silent", raised.exception.code)

    def test_accepts_audible_stereo_audio(self):
        result = short_drama_native_audio.inspect_native_audio(
            "shot.mp4",
            probe=self._probe({"codec": "aac", "sample_rate": 48000, "channels": 2}),
            runner=self._runner(
                "[Parsed_volumedetect_0] mean_volume: -24.3 dB\n"
                "[Parsed_volumedetect_0] max_volume: -3.1 dB\n"
            ),
        )
        self.assertEqual({
            "audible": True,
            "codec": "aac",
            "sample_rate": 48000,
            "channels": 2,
            "mean_volume_dbfs": -24.3,
            "max_volume_dbfs": -3.1,
        }, result)

    def test_rejects_unparseable_volume_output(self):
        with self.assertRaises(short_drama_native_audio.NativeAudioError) as raised:
            short_drama_native_audio.inspect_native_audio(
                "shot.mp4",
                probe=self._probe({"codec": "aac", "sample_rate": 48000, "channels": 2}),
                runner=self._runner("volume data unavailable"),
            )
        self.assertEqual("provider_audio_probe_failed", raised.exception.code)

    def test_native_2k_validation_rejects_low_resolution_video(self):
        with self.assertRaises(short_drama_native_audio.NativeAudioError) as raised:
            short_drama_native_audio.inspect_native_resolution(
                "shot.mp4", "2k", probe=lambda _path: {
                    "video": {"width": 1920, "height": 1080},
                },
            )
        self.assertEqual("provider_resolution_below_2k", raised.exception.code)

    def test_native_2k_validation_accepts_standard_provider_size(self):
        result = short_drama_native_audio.inspect_native_resolution(
            "shot.mp4", "2k", probe=lambda _path: {
                "video": {"width": 2560, "height": 1440},
            },
        )
        self.assertEqual({"width": 2560, "height": 1440}, result)

    def test_native_media_evidence_hashes_the_actual_file(self):
        with tempfile.TemporaryDirectory() as folder:
            media_path = Path(folder) / "raw.mp4"
            raw = b"immutable-provider-bytes"
            media_path.write_bytes(raw)
            with mock.patch.object(
                short_drama_native_audio,
                "inspect_native_resolution",
                return_value={"width": 2560, "height": 1440},
            ), mock.patch.object(
                short_drama_native_audio,
                "inspect_native_audio",
                return_value={"audible": True, "codec": "aac"},
            ):
                evidence = short_drama_native_audio.inspect_native_media(
                    media_path, expected_resolution="2K"
                )
        self.assertEqual(hashlib.sha256(raw).hexdigest(), evidence["sha256"])
        self.assertEqual(len(raw), evidence["size_bytes"])
        self.assertEqual({"width": 2560, "height": 1440}, evidence["resolution"])
        self.assertTrue(evidence["audio"]["audible"])


if __name__ == "__main__":
    unittest.main()
