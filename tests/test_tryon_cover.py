import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class TryonCoverTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        from content_domains import video
        self.video = video

    def _run_case(self, has_clothes, has_background):
        saved = {
            "tryon_person": "video/person.mp4",
            "tryon_cloth": "image/cloth.jpg" if has_clothes else None,
            "tryon_bg": "image/bg.jpg" if has_background else None,
        }
        phases = []

        def fake_save(_data, prefix, _exts):
            return saved[prefix]

        def fake_phase(job_id, phase, **fields):
            phases.append((job_id, phase, fields))

        with patch.object(self.video, "_save_data_file", side_effect=fake_save), \
             patch.object(self.video, "update_video_asset_phase", side_effect=fake_phase), \
             patch.object(self.video, "generate_tryon_video",
                          return_value={"video_file": "video/out.mp4", "video_url": "/api/gen/file/video/out.mp4", "duration": 6}):
            result = self.video.gen_tryon({
                "_job_id": 42,
                "_username": "alice",
                "person_video_data": "person",
                "clothes_data": "cloth" if has_clothes else "",
                "background_data": "bg" if has_background else "",
            })
        return result, phases

    def test_bg_only_uses_background_as_cover(self):
        result, phases = self._run_case(False, True)
        self.assertEqual(result["tryon_mode"], "bg_only")
        self.assertEqual(result["image_file"], "image/bg.jpg")
        self.assertEqual(result["image_url"], "/api/gen/file/image/bg.jpg")
        self.assertEqual(phases[0][2]["image_file"], "image/bg.jpg")

    def test_clothes_only_keeps_clothes_as_cover(self):
        result, phases = self._run_case(True, False)
        self.assertEqual(result["tryon_mode"], "clothes_only")
        self.assertEqual(result["image_file"], "image/cloth.jpg")
        self.assertEqual(phases[0][2]["image_file"], "image/cloth.jpg")

    def test_both_prefers_clothes_as_cover(self):
        result, phases = self._run_case(True, True)
        self.assertEqual(result["tryon_mode"], "both")
        self.assertEqual(result["image_file"], "image/cloth.jpg")
        self.assertEqual(phases[0][2]["image_file"], "image/cloth.jpg")

    def test_runninghub_failure_keeps_task_id_and_reason(self):
        client = types.SimpleNamespace(
            get_status=lambda _task_id: "FAILED",
            query_v2=lambda _task_id: types.SimpleNamespace(
                failed_reason=types.SimpleNamespace(exception_message="input video is invalid"),
                error_message="", error_code="",
            ),
        )
        with patch.object(self.video, "update_video_asset_phase") as phase:
            with self.assertRaisesRegex(RuntimeError, "input video is invalid"):
                self.video._rh_wait_success(client, "rh-123", 42, "tryon_running", "换装失败")
        phase.assert_called_once_with(
            42, "tryon_failed", provider_video_id="rh-123",
            status="error", error="换装失败：input video is invalid",
        )

    def test_silent_tryon_video_gets_audio_before_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "silent.mp4"
            source.write_bytes(b"video")

            def run(command, **_kwargs):
                if command[0] == "ffprobe":
                    return types.SimpleNamespace(stdout="")
                Path(command[-1]).write_bytes(b"video-with-audio")
                return types.SimpleNamespace(stdout="")

            with patch.object(self.video.subprocess, "run", side_effect=run) as runner:
                output = self.video._ensure_tryon_audio(source)
        self.assertTrue(str(output).endswith("_audio.mp4"))
        ffmpeg = runner.call_args_list[1].args[0]
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=44100", ffmpeg)
        self.assertIn("copy", ffmpeg)


if __name__ == "__main__":
    unittest.main()
