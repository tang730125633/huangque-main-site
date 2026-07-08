import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import tikhub


class TikhubAsrTests(unittest.TestCase):
    def test_whisper_openai_timeout_is_bounded_and_clear(self):
        original_key = tikhub.OPENAI_KEY
        original_timeout = tikhub.TRANSCRIBE_TIMEOUT
        tikhub.OPENAI_KEY = "sk-test"
        tikhub.TRANSCRIBE_TIMEOUT = 21
        try:
            with patch.object(tikhub, "_extract_audio", return_value=b"mp3"), \
                 patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
                with self.assertRaises(tikhub.TikHubError) as cm:
                    tikhub._whisper(b"mp4")
            self.assertIn("OpenAI ASR 超时(21s)", str(cm.exception))
        finally:
            tikhub.OPENAI_KEY = original_key
            tikhub.TRANSCRIBE_TIMEOUT = original_timeout


if __name__ == "__main__":
    unittest.main()
