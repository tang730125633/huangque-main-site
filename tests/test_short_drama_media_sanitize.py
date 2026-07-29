import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.content_domains import short_drama_media_sanitize as sanitize


class ShortDramaMediaSanitizeTests(unittest.TestCase):
    def test_command_explicitly_maps_video_and_drops_audio_without_reencoding(self):
        command = sanitize.build_silent_video_command(
            Path("raw.mp4"), Path("silent.mp4")
        )
        self.assertIn("-map", command)
        self.assertEqual("0:v:0", command[command.index("-map") + 1])
        self.assertIn("-an", command)
        self.assertEqual("copy", command[command.index("-c:v") + 1])
        self.assertNotIn("libx264", command)

    def test_sanitizer_preserves_raw_source_and_publishes_verified_silent_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw.mp4"
            destination = root / "silent.mp4"
            source.write_bytes(b"raw-provider-video")
            probes = [
                {
                    "duration_ms": 5000,
                    "video": {"codec": "h264"},
                    "audio": {"codec": "aac", "channels": 2},
                },
                {
                    "duration_ms": 5000,
                    "video": {"codec": "h264"},
                    "audio": None,
                },
            ]

            def runner(command, **_kwargs):
                Path(command[-1]).write_bytes(b"silent-video")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            result = sanitize.sanitize_visual_source(
                source,
                destination,
                runner=runner,
                probe=lambda _path: probes.pop(0),
            )

            self.assertEqual(b"raw-provider-video", source.read_bytes())
            self.assertEqual(b"silent-video", destination.read_bytes())
            self.assertIsNotNone(result["source_report"]["audio"])
            self.assertIsNone(result["silent_report"]["audio"])

    def test_sanitizer_rejects_output_that_still_contains_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw.mp4"
            destination = root / "silent.mp4"
            source.write_bytes(b"raw")

            def runner(command, **_kwargs):
                Path(command[-1]).write_bytes(b"not-silent")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            probe = {
                "duration_ms": 5000,
                "video": {"codec": "h264"},
                "audio": {"codec": "aac"},
            }
            with self.assertRaises(sanitize.MediaSanitizeError) as raised:
                sanitize.sanitize_visual_source(
                    source,
                    destination,
                    runner=runner,
                    probe=lambda _path: probe,
                )
            self.assertEqual("visual_audio_present", raised.exception.code)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
