import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wan_motion_poc.py"
SPEC = importlib.util.spec_from_file_location("wan_motion_poc", SCRIPT)
POC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = POC
SPEC.loader.exec_module(POC)


class WanMotionPocTests(unittest.TestCase):
    def test_builds_official_image_to_action_contract(self):
        payload = POC.build_payload(
            "https://media.example/person.jpg?signature=secret",
            "https://media.example/motion.mp4?signature=secret",
        )
        self.assertEqual(payload["model"], "wan2.2-animate-move")
        self.assertEqual(payload["parameters"]["mode"], "wan-std")
        self.assertEqual(POC.estimate_cost(5, "wan-std"), 2.0)
        preview = POC._preview(payload)
        self.assertNotIn("signature", preview["input"]["image_url"])
        self.assertNotIn("signature", preview["input"]["video_url"])

        temporary = POC.build_payload(
            "oss://dashscope-instant/person.jpg",
            "oss://dashscope-instant/motion.mp4",
        )
        self.assertTrue(temporary["input"]["image_url"].startswith("oss://"))

    def test_rejects_non_https_media(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            POC.build_payload(
                "http://media.example/person.jpg",
                "https://media.example/motion.mp4",
            )

    def test_paid_submit_needs_both_gates(self):
        args = [
            "--identity-image-url", "https://media.example/person.jpg",
            "--motion-video-url", "https://media.example/motion.mp4",
            "--submit",
        ]
        with patch.dict("os.environ", {}, clear=True), patch.object(POC, "submit") as submit:
            with self.assertRaises(SystemExit):
                POC.main(args)
        submit.assert_not_called()

    def test_upload_multipart_keeps_file_last(self):
        body = POC._multipart_body(
            [("policy", "p"), ("key", "k")], SCRIPT, "boundary"
        )
        self.assertLess(body.index(b'name="policy"'), body.index(b'name="file"'))
        self.assertTrue(body.endswith(b"--boundary--\r\n"))


if __name__ == "__main__":
    unittest.main()
