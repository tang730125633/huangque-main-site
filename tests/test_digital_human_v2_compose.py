# -*- coding: utf-8 -*-
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


@unittest.skipUnless(os.name == "posix", "real FFmpeg compositor regression runs on Linux")
class DigitalHumanV2ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server_dir = str(pathlib.Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        from content_domains import core, digital_human_oneclick, digital_human_v2, video
        cls.core = core
        cls.legacy = digital_human_oneclick
        cls.domain = digital_human_v2
        cls.video = video

    @staticmethod
    def _run(arguments):
        subprocess.run(
            arguments, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=180,
        )

    def test_real_mixed_media_compose_has_opening_ending_audio_and_no_black_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            media = root / "fixtures"
            output = root / "videos"
            media.mkdir()
            presenter = media / "presenter.mp4"
            image = media / "material.png"
            clip = media / "material.mp4"
            self._run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=270x480:rate=10:duration=12",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(presenter),
            ])
            self._run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x276749:s=540x960",
                "-frames:v", "1", str(image),
            ])
            self._run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "testsrc=size=270x480:rate=10:duration=4",
                "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                str(clip),
            ])
            payload = {
                "_job_id": 901, "segment_count": 1, "material_count": 2,
                "copy": "真实合成检查，开头结尾真人出镜，中间混合图片和视频素材。",
                "video_files": [presenter.relative_to(root).as_posix()],
                "material_files": [image.relative_to(root).as_posix(), clip.relative_to(root).as_posix()],
                "material_types": ["image", "video"],
                "video_job_ids": [101], "material_job_ids": [0, 0],
            }
            patches = (
                mock.patch.object(self.domain, "OUT_DIR", root),
                mock.patch.object(self.legacy, "OUT_DIR", root),
                mock.patch.object(self.core, "OUT_DIR", root),
                mock.patch.object(self.video, "OUT_DIR", root),
                mock.patch.object(self.video, "VIDEO_OUT_DIR", output),
                mock.patch.object(self.video, "burn_subtitle", side_effect=lambda rel, **_kwargs: rel),
            )
            for patcher in patches:
                patcher.start()
            try:
                result = self.domain.compose(payload)
            finally:
                for patcher in reversed(patches):
                    patcher.stop()
            final = root / result["video_file"]
            self.assertTrue(final.is_file())
            self.assertEqual((result["width"], result["height"]), (1080, 1920))
            self.assertAlmostEqual(result["duration"], 12.0, delta=0.12)
            windows = result["presenter_windows"]
            self.assertEqual(len(windows), 2)
            self.assertEqual(windows[0], [0.0, 3.0])
            self.assertAlmostEqual(windows[1][0], result["duration"] - 3.0, delta=0.08)
            self.assertAlmostEqual(windows[1][1], result["duration"], delta=0.08)
            self.assertEqual(result["verification"]["audio_source"], "continuous_presenter_narration")
            self.assertTrue(result["verification"]["audio_stream"])
            self.assertTrue(result["verification"]["black_frame_check"])

    def test_real_compose_normalizes_public_rgba_image_before_concat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            media = root / "fixtures"
            output = root / "videos"
            media.mkdir()
            presenter = media / "presenter.mp4"
            public_image = media / "public-material.png"
            self._run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=270x480:rate=30:duration=12",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(presenter),
            ])
            self._run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "color=c=0x276749@0.6:s=724x808,format=rgba",
                "-frames:v", "1", str(public_image),
            ])
            payload = {
                "_job_id": 902, "segment_count": 1, "material_count": 1,
                "copy": "Public material may contain alpha and unusual sample aspect ratio.",
                "video_files": [presenter.relative_to(root).as_posix()],
                "material_files": [public_image.relative_to(root).as_posix()],
                "material_types": ["image"],
                "video_job_ids": [201], "material_job_ids": [301],
            }
            patches = (
                mock.patch.object(self.domain, "OUT_DIR", root),
                mock.patch.object(self.legacy, "OUT_DIR", root),
                mock.patch.object(self.core, "OUT_DIR", root),
                mock.patch.object(self.video, "OUT_DIR", root),
                mock.patch.object(self.video, "VIDEO_OUT_DIR", output),
                mock.patch.object(self.video, "burn_subtitle", side_effect=lambda rel, **_kwargs: rel),
            )
            for patcher in patches:
                patcher.start()
            try:
                result = self.domain.compose(payload)
            finally:
                for patcher in reversed(patches):
                    patcher.stop()
            final = root / result["video_file"]
            probe = json.loads(subprocess.check_output([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,pix_fmt,sample_aspect_ratio",
                "-of", "json", str(final),
            ], text=True))
            stream = probe["streams"][0]
            self.assertEqual((stream["width"], stream["height"]), (1080, 1920))
            self.assertEqual(stream["sample_aspect_ratio"], "1:1")
            self.assertEqual(stream["pix_fmt"], "yuv420p")
            self.assertEqual(result["child_jobs"], {"videos": [201], "materials": [301]})

    def test_real_full_presenter_compose_accepts_zero_materials_after_paid_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            media = root / "fixtures"
            output = root / "videos"
            media.mkdir()
            presenter = media / "presenter.mp4"
            self._run([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=270x480:rate=30:duration=6.2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6.2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(presenter),
            ])
            patches = (
                mock.patch.object(self.domain, "OUT_DIR", root),
                mock.patch.object(self.legacy, "OUT_DIR", root),
                mock.patch.object(self.core, "OUT_DIR", root),
                mock.patch.object(self.video, "OUT_DIR", root),
                mock.patch.object(self.video, "VIDEO_OUT_DIR", output),
                mock.patch.object(self.video, "burn_subtitle", side_effect=lambda rel, **_kwargs: rel),
            )
            for patcher in patches:
                patcher.start()
            try:
                for index, duration in enumerate((6.0, 6.05), start=1):
                    states = []
                    payload = {
                        "_job_id": 910 + index, "segment_count": 1, "material_count": 0,
                        "copy": "最短合法视频应当全程使用真人画面。",
                        "video_files": [presenter.relative_to(root).as_posix()],
                        "material_files": [], "material_types": [],
                        "video_job_ids": [201 + index], "material_job_ids": [],
                    }
                    with self.subTest(duration=duration), mock.patch.object(
                            self.video, "_probe_video_duration", return_value=duration):
                        result = self.domain.compose(
                            payload,
                            persist_state=lambda state, **_kwargs: states.append(state),
                        )
                        self.assertEqual(result["material_count"], 0)
                        self.assertEqual(result["child_jobs"]["videos"], [201 + index])
                        self.assertEqual(result["presenter_windows"], [[0.0, duration]])
                        self.assertAlmostEqual(result["duration"], duration, delta=0.12)
                        self.assertEqual(states[-1], "completed")
            finally:
                for patcher in reversed(patches):
                    patcher.stop()


if __name__ == "__main__":
    unittest.main()
