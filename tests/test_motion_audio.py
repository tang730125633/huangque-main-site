# -*- coding: utf-8 -*-
"""Movie-avatar reference video audio policy.

Open generation uploads reference videos unchanged and never extracts, strips,
or restores their audio. Motion imitation keeps the existing audio round trip.
The shared helpers remain because Seedance upscale also restores source audio.
"""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

os.environ.setdefault("CONTENT_BASE", tempfile.mkdtemp())
video = importlib.import_module("content_domains.video")
SRC = (ROOT / "server/content_domains/video.py").read_text(encoding="utf-8")
GEN = SRC.split("def gen_cinematic")[1].split("\ndef ")[0]
PREP = SRC.split("def _prepare_cinematic_reference_videos")[1].split("\ndef ")[0]


class OpenGenerationAudioTests(unittest.TestCase):
    def test_open_uploads_original_references_without_audio_processing(self):
        refs = ["video/one.mp4", "video/two.mp4"]
        with patch.object(video, "_extract_reference_audio") as extract, \
             patch.object(video, "_strip_audio") as strip:
            prepared, source_audio = video._prepare_cinematic_reference_videos(refs, "open")
        self.assertEqual(refs, prepared)
        self.assertIsNone(source_audio)
        extract.assert_not_called()
        strip.assert_not_called()

    def test_open_path_cannot_restore_reference_audio(self):
        self.assertIn('if cine_mode == "open":', PREP)
        self.assertIn("return files, None", PREP)
        self.assertIn("_prepare_cinematic_reference_videos(", GEN)
        self.assertIn("if source_audio:", GEN)


class MotionImitationAudioTests(unittest.TestCase):
    def test_motion_extracts_first_reference_then_strips_all_references(self):
        refs = ["video/one.mp4", "video/two.mp4"]
        with patch.object(video, "_extract_reference_audio", return_value="audio/source.m4a") as extract, \
             patch.object(video, "_strip_audio", side_effect=lambda value: value + ".silent") as strip:
            prepared, source_audio = video._prepare_cinematic_reference_videos(refs, "motion")
        self.assertEqual(["video/one.mp4.silent", "video/two.mp4.silent"], prepared)
        self.assertEqual("audio/source.m4a", source_audio)
        extract.assert_called_once_with("video/one.mp4")
        self.assertEqual([call(value) for value in refs], strip.call_args_list)

    def test_the_video_stream_is_copied_not_re_encoded_when_restoring_audio(self):
        block = SRC.split("def _mux_original_audio")[1].split("\ndef ")[0]
        self.assertIn('"-c:v", "copy"', block)
        self.assertIn('"-shortest"', block)


class NoTranscodingTests(unittest.TestCase):
    def test_the_cinematic_path_does_not_shrink_references(self):
        self.assertNotIn("_shrink_motion_reference", GEN)

    def test_stripping_audio_does_not_re_encode(self):
        block = SRC.split("def _strip_audio")[1].split("\ndef ")[0]
        self.assertIn('"-c:v", "copy"', block)
        self.assertNotIn("libx264", block)


class AudioHelperFallbackTests(unittest.TestCase):
    def test_a_failed_strip_uploads_the_original(self):
        with patch.object(video.subprocess, "run", side_effect=RuntimeError("ffmpeg missing")):
            got = video._strip_audio("/tmp/whatever.mp4")
        self.assertEqual(got, "/tmp/whatever.mp4")

    def test_a_reference_without_audio_is_not_an_error(self):
        with patch.object(video, "_resolve_out_file", return_value=None):
            self.assertIsNone(video._extract_reference_audio("video/x.mp4"))

    def test_a_failed_mux_keeps_the_silent_video(self):
        with patch.object(video, "_resolve_out_file", return_value=None):
            got = video._mux_original_audio("video/out.mp4", "audio/src.m4a")
        self.assertEqual(got, "video/out.mp4")


if __name__ == "__main__":
    unittest.main()
