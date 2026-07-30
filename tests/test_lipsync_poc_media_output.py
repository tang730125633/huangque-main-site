import tempfile
import unittest
from pathlib import Path

from tools.lipsync_poc.metrics.media_output import (
    MediaOutputError,
    build_strip_audio_command,
    ensure_silent_video,
)


class Completed:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stderr = ""


class LipsyncPocMediaOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "result.mp4"
        self.media.write_bytes(b"with-audio")

    def tearDown(self):
        self.temp.cleanup()

    def test_audio_is_removed_atomically_and_hash_is_recorded(self):
        probes = []

        def probe(path):
            path = Path(path)
            probes.append(path)
            return {
                "video_stream_count": 1,
                "audio_stream_count": 0
                if path.name.endswith(".silent.part.mp4") else 1,
            }

        def run(command, **kwargs):
            destination = Path(command[-1])
            destination.write_bytes(b"silent-video")
            return Completed()

        result = ensure_silent_video(
            self.media,
            probe,
            runner=run,
        )
        self.assertTrue(result["audio_removed"])
        self.assertEqual(1, result["source_audio_stream_count"])
        self.assertEqual(b"silent-video", self.media.read_bytes())
        self.assertEqual(64, len(result["output_sha256"]))
        self.assertFalse(
            self.media.with_suffix(".silent.part.mp4").exists()
        )
        command = build_strip_audio_command("in.mp4", "out.mp4")
        self.assertIn("-an", command)
        self.assertNotIn("shell=True", command)

    def test_existing_silent_video_does_not_invoke_ffmpeg(self):
        result = ensure_silent_video(
            self.media,
            lambda _: {
                "video_stream_count": 1,
                "audio_stream_count": 0,
            },
            runner=lambda *args, **kwargs: self.fail("ffmpeg was called"),
        )
        self.assertFalse(result["audio_removed"])

    def test_failed_audio_removal_does_not_replace_download(self):
        with self.assertRaises(MediaOutputError):
            ensure_silent_video(
                self.media,
                lambda _: {
                    "video_stream_count": 1,
                    "audio_stream_count": 1,
                },
                runner=lambda *args, **kwargs: Completed(returncode=1),
            )
        self.assertEqual(b"with-audio", self.media.read_bytes())


if __name__ == "__main__":
    unittest.main()
