import sys
import tempfile
import unittest
from pathlib import Path


class VideoFaststartTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        from content_domains import video

        self.video = video
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_out_path = video._out_path
        self.old_run = video.subprocess.run
        video._out_path = lambda rel: self.root / rel

    def tearDown(self):
        self.video._out_path = self.old_out_path
        self.video.subprocess.run = self.old_run
        self.tmp.cleanup()

    def test_faststart_replaces_original_mp4(self):
        src = self.root / "video" / "done.mp4"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"original")

        def fake_run(cmd, **kwargs):
            self.assertIn("-movflags", cmd)
            self.assertIn("+faststart", cmd)
            Path(cmd[-1]).write_bytes(b"faststart")

        self.video.subprocess.run = fake_run

        rel = self.video._faststart_video_file("video/done.mp4")

        self.assertEqual(rel, "video/done.mp4")
        self.assertEqual(src.read_bytes(), b"faststart")

    def test_faststart_missing_ffmpeg_keeps_original(self):
        src = self.root / "video" / "done.mp4"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"original")

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("ffmpeg")

        self.video.subprocess.run = fake_run

        rel = self.video._faststart_video_file("video/done.mp4")

        self.assertEqual(rel, "video/done.mp4")
        self.assertEqual(src.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
