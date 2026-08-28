import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)


class DigitalHumanSubtitlePreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.video = importlib.import_module("content_domains.video")

    def test_preflight_loads_the_local_stack_once_and_never_charges(self):
        with tempfile.TemporaryDirectory() as raw:
            font = Path(raw) / "NotoSansCJK-Regular.ttc"
            font.write_bytes(b"font")
            charset = " ".join(
                "%04x" % ord(character)
                for character in self.video.SUBTITLE_REQUIRED_CJK_GLYPHS
            )
            with mock.patch.object(
                    self.video, "_subtitle_runtime_ready", False), \
                 mock.patch.object(
                    self.video.shutil, "which", return_value="/usr/bin/tool"), \
                 mock.patch.object(self.video, "VIDEO_OUT_DIR", Path(raw)), \
                 mock.patch.object(
                    self.video, "_subtitle_tool_output", side_effect=[
                        " V..... libx264 H.264\n A..... aac AAC\n",
                        " T.C drawtext\n ... subtitles\n",
                        "Noto Sans CJK SC\n%s\n" % font,
                        charset,
                    ],
                 ) as tools, mock.patch.object(
                    self.video, "_get_whisper_model", return_value=object(),
                 ) as model:
                first = self.video.subtitle_runtime_preflight()
                second = self.video.subtitle_runtime_preflight()
        self.assertEqual(first, second)
        self.assertTrue(first["ok"])
        self.assertTrue(first["no_charge"])
        self.assertEqual(4, tools.call_count)
        model.assert_called_once_with()

    def test_preflight_rejects_missing_whisper_before_paid_submission(self):
        with tempfile.TemporaryDirectory() as raw:
            font = Path(raw) / "NotoSansCJK-Regular.ttc"
            font.write_bytes(b"font")
            charset = " ".join(
                "%04x" % ord(character)
                for character in self.video.SUBTITLE_REQUIRED_CJK_GLYPHS
            )
            with mock.patch.object(
                    self.video, "_subtitle_runtime_ready", False), \
                 mock.patch.object(
                    self.video.shutil, "which", return_value="/usr/bin/tool"), \
                 mock.patch.object(self.video, "VIDEO_OUT_DIR", Path(raw)), \
                 mock.patch.object(
                    self.video, "_subtitle_tool_output", side_effect=[
                        " V..... libx264 H.264\n A..... aac AAC\n",
                        " T.C drawtext\n ... subtitles\n",
                        "Noto Sans CJK SC\n%s\n" % font,
                        charset,
                    ],
                 ), mock.patch.object(
                    self.video, "_get_whisper_model",
                    side_effect=ModuleNotFoundError("faster_whisper"),
                 ):
                with self.assertRaises(
                        self.video.SubtitleRuntimePreflightError) as caught:
                    self.video.subtitle_runtime_preflight()
        self.assertEqual("subtitle_runtime_unavailable", caught.exception.code)
        self.assertIn("未调用付费视频渠道", str(caught.exception))

    def test_repository_declares_the_offline_whisper_runtime(self):
        requirements = (ROOT / "deploy" / "requirements-content.txt").read_text(
            encoding="utf-8",
        )
        dropin = (
            ROOT / "deploy" / "systemd" / "huangque-content.service.d"
            / "whisper.conf"
        ).read_text(encoding="utf-8")
        self.assertIn("faster-whisper==1.2.1", requirements)
        for marker in (
            "WHISPER_MODEL=small",
            "WHISPER_DEVICE=cpu",
            "WHISPER_COMPUTE_TYPE=int8",
            "WHISPER_CACHE_DIR=/home/ubuntu/.cache/huggingface/hub",
            'Environment="SUBTITLE_FONT=Noto Sans SC"',
            "HF_HUB_OFFLINE=1",
        ):
            self.assertIn(marker, dropin)


if __name__ == "__main__":
    unittest.main()
