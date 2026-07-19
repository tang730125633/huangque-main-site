# -*- coding: utf-8 -*-
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import audio, core


class AudioSubmissionHardeningTests(unittest.TestCase):
    def test_rejects_question_mark_encoding_damage(self):
        with self.assertRaisesRegex(ValueError, "编码异常"):
            audio.validate_audio_payload({"text": "?" * 30})

    def test_rejects_unicode_replacement_character(self):
        with self.assertRaisesRegex(ValueError, "编码异常"):
            audio.validate_audio_payload({"text": "今天的内容\ufffd需要重新粘贴"})

    def test_keeps_normal_text_and_question_punctuation(self):
        payload = audio.validate_audio_payload({"prompt": "今天为什么要做好内容？你知道吗?"})
        self.assertEqual("今天为什么要做好内容？你知道吗?", payload["text"])

    def test_transient_cosyvoice_failure_retries_once(self):
        with tempfile.TemporaryDirectory() as td:
            output = pathlib.Path(td) / "result.mp3"
            with patch.object(audio, "resolve_audio_provider_voice", return_value="S_d21F8OR62"), \
                    patch.object(audio.cosyvoice, "enabled", return_value=True), \
                    patch.object(audio.cosyvoice, "synth", side_effect=[ConnectionError("closed"), b"mp3"]) as synth, \
                    patch.object(audio.time, "sleep") as sleep, \
                    patch.object(audio, "_out_path", return_value=output), \
                    patch.object(audio, "public_url", return_value="https://cos/result.mp3"):
                result = audio.gen_audio({"text": "这是一段正常的配音测试文案", "voice": "S_d21F8OR62", "_username": "alice"})
        self.assertEqual("https://cos/result.mp3", result["url"])
        self.assertEqual(2, synth.call_count)
        sleep.assert_called_once_with(0.5)

    def test_invalid_parameter_is_not_retried(self):
        with patch.object(audio, "resolve_audio_provider_voice", return_value="S_d21F8OR62"), \
                patch.object(audio.cosyvoice, "enabled", return_value=True), \
                patch.object(audio.cosyvoice, "synth", side_effect=RuntimeError("InvalidParameter: bad text")) as synth:
            with self.assertRaisesRegex(RuntimeError, "InvalidParameter"):
                audio.gen_audio({"text": "正常输入仍由上游拒绝", "voice": "S_d21F8OR62", "_username": "alice"})
        self.assertEqual(1, synth.call_count)

    def test_queue_health_reports_depth_capacity_and_inflight(self):
        with patch.object(core._talking_job_queue, "qsize", return_value=7), \
                patch.object(core._talking_job_queue, "maxsize", 32), \
                patch.object(core, "_inflight", 10):
            queues, inflight = core._queue_health()
        self.assertEqual({"depth": 7, "capacity": 32, "remaining": 25}, queues["talking"])
        self.assertEqual(10, inflight)


if __name__ == "__main__":
    unittest.main()
