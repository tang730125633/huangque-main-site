import sys
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


if __name__ == "__main__":
    unittest.main()
