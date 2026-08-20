import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "seedance_motion_poc.py"
SPEC = importlib.util.spec_from_file_location("seedance_motion_poc", SCRIPT)
POC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = POC
SPEC.loader.exec_module(POC)


class SeedanceMotionPocTests(unittest.TestCase):
    def test_three_modes_build_the_expected_multimodal_contract(self):
        for mode, expected_images in (("rgb", 1), ("depth", 1), ("depth_scene", 2)):
            with self.subTest(mode=mode):
                payload = POC.build_payload(
                    mode,
                    "asset://asset-authorized-person",
                    "https://media.example/motion.mp4",
                    "https://media.example/scene.jpg" if mode == "depth_scene" else None,
                )
                self.assertEqual(
                    [item["type"] for item in payload["content"]],
                    ["text"] + ["image_url"] * expected_images + ["video_url"],
                )
                self.assertEqual(payload["content"][-1]["role"], "reference_video")

    def test_real_person_requires_an_authorized_asset(self):
        with self.assertRaisesRegex(ValueError, "本人授权"):
            POC.build_payload(
                "rgb", "https://media.example/person.jpg",
                "https://media.example/motion.mp4",
            )

    def test_paid_submit_is_blocked_without_explicit_environment_gate(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(
                POC.video_seedance, "generate") as generate:
            with self.assertRaises(SystemExit):
                POC.main([
                    "--identity-image", "asset://asset-authorized-person",
                    "--motion-video", "https://media.example/motion.mp4",
                    "--submit",
                ])
        generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
