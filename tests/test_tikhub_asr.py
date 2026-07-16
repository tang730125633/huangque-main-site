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
        # _whisper 现在收的是【落盘的 mp4 路径】：视频流式落盘后，ffmpeg 直接读文件，
        # 不必先把 100MB 读回内存（见 tikhub.download_to_file 的注释）。
        import os as _os, tempfile as _tempfile
        fd, mp4_path = _tempfile.mkstemp(suffix=".mp4")
        _os.write(fd, b"mp4"); _os.close(fd)
        try:
            with patch.object(tikhub, "_extract_audio", return_value=b"mp3"), \
                 patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
                with self.assertRaises(tikhub.TikHubError) as cm:
                    tikhub._whisper(mp4_path)
            self.assertIn("OpenAI ASR 超时(21s)", str(cm.exception))
        finally:
            tikhub.OPENAI_KEY = original_key
            tikhub.TRANSCRIBE_TIMEOUT = original_timeout
            _os.unlink(mp4_path)


if __name__ == "__main__":
    unittest.main()
