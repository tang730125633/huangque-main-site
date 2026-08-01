import os
import unittest
from unittest import mock

from server.content_domains import short_drama_visual_gate as gate


class ShortDramaVisualGateTests(unittest.TestCase):
    def test_gate_is_off_by_default_and_invalid_values_fail_closed(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual("off", gate.gate_mode())
        with mock.patch.dict(
            os.environ, {"SHORT_DRAMA_VISUAL_GATE_MODE": "invalid"},
            clear=True,
        ):
            self.assertEqual("off", gate.gate_mode())

    def test_default_off_performs_no_media_or_external_work(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(gate, "_transcribe_source_audio") as asr,
            mock.patch.object(gate, "_extract_frames") as frames,
            mock.patch.object(gate, "_inspect_frames") as vision,
        ):
            report = gate.inspect_visual_source(
                "source.mp4", "silent.mp4",
                {"source": {"audio": {"codec": "aac"}}}, {},
            )
        self.assertEqual("skipped", report["decision"])
        asr.assert_not_called()
        frames.assert_not_called()
        vision.assert_not_called()

    def test_clean_silent_video_is_accepted(self):
        report = gate.build_gate_report(
            "enforce",
            {"source": {"video": {"codec": "h264"}, "audio": None}},
            {"status": "not_applicable", "transcript": "", "segments": []},
            {
                "status": "done", "passed": True, "confidence": 0.94,
                "character_match": 0.95, "scene_match": 0.91,
                "action_match": 0.9, "camera_match": 0.9,
                "visible_speech": False, "generated_text": False,
                "reasons": [],
            },
        )
        self.assertEqual("accepted", report["decision"])
        self.assertFalse(report["blocking"])

    def test_detected_provider_speech_blocks_enforce_mode(self):
        report = gate.build_gate_report(
            "enforce",
            {"source": {"video": {"codec": "h264"}, "audio": {"codec": "aac"}}},
            {
                "status": "done", "transcript": "This is invented speech",
                "segments": [{"start": 0, "end": 1, "text": "invented"}],
            },
            {"status": "done", "passed": True, "confidence": 0.9},
        )
        self.assertEqual("rejected_visual", report["decision"])
        self.assertTrue(report["blocking"])
        self.assertIn("generated_speech_detected", report["codes"])

    def test_shadow_mode_records_visible_speech_without_blocking(self):
        report = gate.build_gate_report(
            "shadow",
            {"source": {"video": {}, "audio": None}},
            {"status": "not_applicable", "transcript": "", "segments": []},
            {
                "status": "done", "passed": False, "confidence": 0.93,
                "character_match": 0.9, "scene_match": 0.9,
                "action_match": 0.5, "camera_match": 0.9,
                "visible_speech": True, "generated_text": False,
                "reasons": ["人物持续张嘴说话"],
            },
        )
        self.assertEqual("rejected_visual", report["decision"])
        self.assertFalse(report["blocking"])
        self.assertIn("visible_speech_detected", report["codes"])

    def test_unavailable_validator_requires_manual_review(self):
        report = gate.build_gate_report(
            "enforce",
            {"source": {"video": {}, "audio": None}},
            {"status": "not_applicable", "transcript": "", "segments": []},
            {"status": "unavailable", "error": "temporary outage"},
        )
        self.assertEqual("manual_review", report["decision"])
        self.assertFalse(report["blocking"])

    def test_inspection_returns_blocking_report_instead_of_raising(self):
        original_mode = gate.gate_mode
        original_asr = gate._transcribe_source_audio
        original_frames = gate._extract_frames
        original_inspect = gate._inspect_frames
        gate.gate_mode = lambda: "enforce"
        gate._transcribe_source_audio = lambda path: {
            "status": "done", "transcript": "invented provider speech",
            "segments": [],
        }
        gate._extract_frames = lambda path: (None, ["frame"])
        gate._inspect_frames = lambda frames, spec: {
            "status": "done", "passed": True, "confidence": 0.95,
        }
        try:
            report = gate.inspect_visual_source(
                "source.mp4", "silent.mp4",
                {"source": {"audio": {"codec": "aac"}}}, {},
            )
        finally:
            gate.gate_mode = original_mode
            gate._transcribe_source_audio = original_asr
            gate._extract_frames = original_frames
            gate._inspect_frames = original_inspect
        self.assertEqual("rejected_visual", report["decision"])
        self.assertTrue(report["blocking"])


if __name__ == "__main__":
    unittest.main()
