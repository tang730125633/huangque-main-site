import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


import sys

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import short_drama_assembly_plan as plan


class ShortDramaAssemblyPlanTests(unittest.TestCase):
    def test_canonical_hash_is_stable_and_sensitive(self):
        left = {"ratio": "9:16", "shots": [{"id": "s1", "duration_ms": 5000}]}
        right = {"shots": [{"duration_ms": 5000, "id": "s1"}], "ratio": "9:16"}
        self.assertEqual(plan.canonical_hash(left), plan.canonical_hash(right))
        right["shots"][0]["duration_ms"] = 5001
        self.assertNotEqual(plan.canonical_hash(left), plan.canonical_hash(right))

    def test_duration_action_uses_200ms_tolerance(self):
        self.assertEqual("keep", plan.duration_action(5000, 5000))
        self.assertEqual("keep", plan.duration_action(4800, 5000))
        self.assertEqual("trim_tail", plan.duration_action(5300, 5000))
        self.assertEqual("freeze_last_frame", plan.duration_action(4700, 5000))

    def test_timeline_is_sorted_before_overlap_validation(self):
        lines = [
            {
                "id": "line-later", "start_ms": 500, "end_ms": 600,
                "audio_duration_ms": 100, "subtitle_visible": True,
                "subtitle_text": "后",
            },
            {
                "id": "line-first", "start_ms": 0, "end_ms": 100,
                "audio_duration_ms": 100, "subtitle_visible": True,
                "subtitle_text": "前",
            },
        ]
        normalized, blockers = plan.validate_timeline(lines, 1000)
        self.assertEqual(["line-first", "line-later"],
                         [item["id"] for item in normalized])
        self.assertEqual([], blockers)

    def test_timeline_reports_audio_subtitle_and_bounds_errors(self):
        lines = [
            {
                "id": "a", "start_ms": 0, "end_ms": 700,
                "audio_duration_ms": 700, "subtitle_visible": True,
                "subtitle_text": "甲",
            },
            {
                "id": "b", "start_ms": 600, "end_ms": 1100,
                "audio_duration_ms": 500, "subtitle_visible": True,
                "subtitle_text": "乙",
            },
        ]
        _normalized, blockers = plan.validate_timeline(lines, 1000)
        self.assertEqual(
            {"timeline_invalid", "audio_overlap", "subtitle_overlap"},
            {item["code"] for item in blockers},
        )

    def test_controlled_file_resolver_rejects_traversal_and_outside_files(self):
        with tempfile.TemporaryDirectory(
            prefix=".tmp-short-drama-plan-", dir=ROOT
        ) as temp:
            output = Path(temp) / "out"
            output.mkdir()
            media = output / "video" / "clip.mp4"
            media.parent.mkdir()
            media.write_bytes(b"media")
            self.assertEqual(media.resolve(),
                             plan.resolve_controlled_file("video/clip.mp4", output))
            for value in ("../secret.mp4", str(Path(temp) / "outside.mp4")):
                with self.assertRaises(plan.MediaPlanError) as raised:
                    plan.resolve_controlled_file(value, output)
                self.assertEqual("source_file_untrusted", raised.exception.code)
            with self.assertRaises(plan.MediaPlanError) as missing:
                plan.resolve_controlled_file("", output)
            self.assertEqual("missing_source_file", missing.exception.code)

    def test_probe_parses_rotation_fps_and_audio_without_shell(self):
        payload = {
            "streams": [
                {
                    "codec_type": "video", "codec_name": "h264",
                    "width": 1080, "height": 1920, "pix_fmt": "yuv420p",
                    "sample_aspect_ratio": "1:1", "avg_frame_rate": "30000/1001",
                    "tags": {"rotate": "90"},
                },
                {
                    "codec_type": "audio", "codec_name": "aac",
                    "sample_rate": "48000", "channels": 2,
                },
            ],
            "format": {"duration": "5.125"},
        }
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        result = plan.probe_media(Path("clip.mp4"), runner=runner)
        self.assertEqual(5125, result["duration_ms"])
        self.assertEqual(90, result["video"]["rotation"])
        self.assertAlmostEqual(30000 / 1001, result["video"]["fps"], places=3)
        self.assertEqual(48000, result["audio"]["sample_rate"])
        self.assertNotIn("shell", calls[0][1])
        self.assertEqual("ffprobe", calls[0][0][0])

    def test_probe_prefers_display_matrix_rotation_and_normalizes_it(self):
        payload = {
            "streams": [{
                "codec_type": "video", "codec_name": "h264",
                "width": 1920, "height": 1080,
                "avg_frame_rate": "30/1",
                "tags": {"rotate": "180"},
                "side_data_list": [{
                    "side_data_type": "Display Matrix",
                    "rotation": -90,
                }],
            }],
            "format": {"duration": "5"},
        }

        def runner(_args, **_kwargs):
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(payload), stderr=""
            )

        result = plan.probe_media(Path("rotated.mp4"), runner=runner)
        self.assertEqual(270, result["video"]["rotation"])
        self.assertEqual((1080, 1920), plan.dimensions_for_ratio(result))
        self.assertTrue(plan.ratio_matches(result, "9:16"))

        payload["streams"][0]["side_data_list"][0]["rotation"] = "invalid"
        result = plan.probe_media(Path("fallback.mp4"), runner=runner)
        self.assertEqual(180, result["video"]["rotation"])
        self.assertEqual((1920, 1080), plan.dimensions_for_ratio(result))

    def test_probe_failure_codes_are_stable(self):
        def missing(_args, **_kwargs):
            raise FileNotFoundError()

        with self.assertRaises(plan.MediaPlanError) as unavailable:
            plan.probe_media(Path("clip.mp4"), runner=missing)
        self.assertEqual("ffprobe_unavailable", unavailable.exception.code)

        def failed(_args, **_kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="bad input")

        with self.assertRaises(plan.MediaPlanError) as invalid:
            plan.probe_media(Path("clip.mp4"), runner=failed)
        self.assertEqual("media_probe_failed", invalid.exception.code)

    def test_ffprobe_version_gate(self):
        def supported(_args, **_kwargs):
            return SimpleNamespace(
                returncode=0, stdout="ffprobe version 6.1\n", stderr=""
            )

        self.assertEqual(
            "ffprobe version 6.1",
            plan.inspect_ffprobe(runner=supported),
        )

        def old(_args, **_kwargs):
            return SimpleNamespace(
                returncode=0, stdout="ffprobe version 3.2\n", stderr=""
            )

        with self.assertRaises(plan.MediaPlanError) as raised:
            plan.inspect_ffprobe(runner=old)
        self.assertEqual("media_probe_failed", raised.exception.code)

    def test_file_fingerprint_contains_content_hash(self):
        with tempfile.TemporaryDirectory(
            prefix=".tmp-short-drama-plan-", dir=ROOT
        ) as temp:
            media = Path(temp) / "clip.bin"
            media.write_bytes(b"stable media")
            fingerprint = plan.file_fingerprint(media)
        self.assertEqual(
            hashlib.sha256(b"stable media").hexdigest(),
            fingerprint["sha256"],
        )
        self.assertEqual(len(b"stable media"), fingerprint["size"])

    def test_normalization_plan_is_deterministic(self):
        shots = [
            {
                "id": "s1", "duration_ms": 5000,
                "video_probe": {
                    "duration_ms": 5300,
                    "video": {"width": 1080, "height": 1920, "fps": 24.0},
                },
                "voice_lines": [],
            },
            {
                "id": "s2", "duration_ms": 5000,
                "video_probe": {
                    "duration_ms": 4700,
                    "video": {"width": 1080, "height": 1920, "fps": 30.0},
                },
                "voice_lines": [],
            },
        ]
        first = plan.build_normalization_plan("9:16", 10, shots)
        second = plan.build_normalization_plan("9:16", 10, list(reversed(shots)))
        self.assertEqual(first, second)
        self.assertEqual("trim_tail", first["shots"][0]["video"]["duration_action"])
        self.assertEqual(
            "freeze_last_frame",
            first["shots"][1]["video"]["duration_action"],
        )
        self.assertEqual({"width": 1080, "height": 1920},
                         first["profiles"]["final"]["resolution"])
        self.assertEqual(10000, first["project_duration_ms"])


if __name__ == "__main__":
    unittest.main()
