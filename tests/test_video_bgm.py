import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from content_domains import video


class VideoBgmTests(unittest.TestCase):
    def test_validation_rejects_bad_volume_and_audio(self):
        base = {"mode": "text", "image_data": "data:image/png;base64,iVBORw0KGgo=", "text": "x", "voice": "v"}
        with self.assertRaisesRegex(ValueError, "bgm_volume"):
            video.validate_video_payload(dict(base, bgm_volume=2))
        with self.assertRaisesRegex(ValueError, "bgm_data"):
            video.validate_video_payload(dict(base, bgm_data="data:text/plain;base64,eA=="))

    def test_mix_retries_without_source_audio(self):
        out = Mock()
        out.is_file.return_value = True
        out.stat.return_value.st_size = 10
        with patch.object(video, "_resolve_out_file", side_effect=[pathlib.Path("v.mp4"), pathlib.Path("b.mp3")]), \
             patch.object(video, "_out_path", return_value=out), \
             patch.object(video.subprocess, "run", side_effect=[RuntimeError("no source audio"), None]) as run:
            result = video.mix_video_bgm("video/v.mp4", "audio/b.mp3", 0.18)
        self.assertTrue(result.startswith("video/bgm_"))
        self.assertEqual(2, run.call_count)
        self.assertIn("amix=inputs=2", " ".join(run.call_args_list[0].args[0]))
        self.assertNotIn("amix=inputs=2", " ".join(run.call_args_list[1].args[0]))

    def test_no_bgm_does_not_consume_an_extra_file_save(self):
        with patch.object(video, "HEYGEN_API_KEY", "configured"), \
             patch.object(video, "_save_data_file", side_effect=["image.png", "reference.mp4"]) as save, \
             patch.object(video, "_motion_reference_duration", return_value=6), \
             patch.object(video, "generate_heygen_motion_video", return_value={"video_file": "video/out.mp4"}), \
             patch.object(video, "public_url", return_value="/video/out.mp4"), \
             patch.object(video, "update_video_asset_phase"):
            video.gen_video({"mode": "motion", "line": "1", "image_data": "image", "reference_video_data": "video"})
        self.assertEqual(2, save.call_count)

    def test_frontend_sends_bgm_on_talking_batch_and_motion(self):
        html = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")
        self.assertIn('id="bgmPanel"', html)
        self.assertIn("bgm_data:bgmData,bgm_volume:selectedBgmVolume", html)
        self.assertIn("body.bgm_data=bgmData; body.bgm_volume=selectedBgmVolume;", html)
        self.assertIn("var body=talkingPayload({});", html)


if __name__ == "__main__":
    unittest.main()
