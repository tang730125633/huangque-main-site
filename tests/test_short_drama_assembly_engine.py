import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


import sys

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import short_drama_assembly_engine as engine


def _media_plan():
    return {
        "project_duration_ms": 10000,
        "shots": [
            {
                "id": "shot-1", "start_ms": 0, "end_ms": 5000,
                "duration_ms": 5000,
                "audio": {"lines": [{
                    "id": "line-1", "start_ms": 100, "end_ms": 900,
                    "audio_duration_ms": 800,
                    "subtitle_visible": True, "subtitle_text": "第一句",
                }]},
            },
            {
                "id": "shot-2", "start_ms": 5000, "end_ms": 10000,
                "duration_ms": 5000,
                "audio": {"lines": []},
            },
        ],
    }


def _silent_media_plan():
    value = _media_plan()
    value["shots"][0]["audio"]["lines"] = []
    return value


class _FakeMedia:
    def __init__(self, fail_after=None):
        self.calls = []
        self.fail_after = fail_after

    def runner(self, args, **_kwargs):
        self.calls.append(list(args))
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            return SimpleNamespace(returncode=1, stdout="", stderr="failed")
        if args[-1] == "-":
            measured = {
                "input_i": "-18.0", "input_tp": "-2.5",
                "input_lra": "4.0", "input_thresh": "-28.0",
                "target_offset": "0.1",
            }
            return SimpleNamespace(
                returncode=0, stdout="", stderr=json.dumps(measured)
            )
        output = Path(args[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(("fake:" + output.name).encode("utf-8"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @staticmethod
    def probe(path):
        path = Path(path)
        duration = 5000 if path.parent.name == "shots" else 10000
        return {
            "duration_ms": duration,
            "video": None,
            "audio": {"sample_rate": 48000, "channels": 2, "codec": "pcm_s16le"},
        }


class ShortDramaAssemblyEngineTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(
            prefix=".tmp-short-drama-d2-engine-", dir=ROOT
        )
        self.output_root = Path(self.tempdir.name) / "content_out"
        self.output_root.mkdir()
        self.voice = Path(self.tempdir.name) / "voice.wav"
        self.voice.write_bytes(b"voice")
        self.hash = "a" * 64

    def tearDown(self):
        self.tempdir.cleanup()

    def test_bundle_builds_audio_ass_manifest_and_relative_artifacts(self):
        fake = _FakeMedia()
        checks = []
        result = engine.build_bundle(
            output_root=self.output_root,
            project_id="project-1",
            d1_input_hash="d1-hash",
            input_hash=self.hash,
            ratio="9:16",
            config={
                "subtitle": {"enabled": True, "position": "bottom"},
                "bgm": {
                    "asset_id": None, "volume": 0.18,
                    "fade_in_ms": 500, "fade_out_ms": 800,
                },
            },
            media_plan=_media_plan(),
            shot_inputs={
                "shot-1": [{"id": "line-1", "start_ms": 100, "file": self.voice}],
                "shot-2": [],
            },
            runner=fake.runner,
            probe=fake.probe,
            identity_check=lambda: checks.append(True) or True,
            claim_token="1" * 32,
            claim_check=lambda: True,
            toolchain={"ffmpeg": "fake-1", "ffprobe": "fake-1", "font": "test"},
        )
        self.assertEqual(2, len(checks))
        self.assertTrue((self.output_root / result["directory"]).is_dir())
        kinds = {item["kind"] for item in result["artifacts"]}
        self.assertEqual(
            {"shot_voice", "dialogue", "master_audio",
             "subtitles_ass", "manifest"},
            kinds,
        )
        self.assertNotIn("bgm", kinds)
        for item in result["artifacts"]:
            self.assertFalse(Path(item["file"]).is_absolute())
            self.assertEqual(64, len(item["file_hash"]))
            self.assertTrue((self.output_root / item["file"]).is_file())
        manifest = json.loads(
            (self.output_root / result["directory"] / "manifest.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual("short_drama_audio_subtitle_v1",
                         manifest["engine_version"])
        self.assertEqual("d1-hash", manifest["d1_input_hash"])
        self.assertEqual(self.hash, manifest["input_hash"])
        self.assertEqual(1, manifest["subtitle_events"])

    def test_bundle_reuses_audio_cache_when_only_subtitle_bundle_changes(self):
        cache = Path(self.tempdir.name) / "audio-cache"
        (cache / "shots").mkdir(parents=True)
        cached = {
            ("shot_voice", "shot-1"): cache / "shots" / "shot-1.wav",
            ("shot_voice", "shot-2"): cache / "shots" / "shot-2.wav",
            ("dialogue", ""): cache / "dialogue.wav",
            ("master_audio", ""): cache / "master.wav",
        }
        for path in cached.values():
            path.write_bytes(("cached:" + path.name).encode("utf-8"))
        cached = {
            key: {
                "path": path,
                "file_hash": engine._hash_file(path),
            }
            for key, path in cached.items()
        }
        fake = _FakeMedia()
        result = engine.build_bundle(
            output_root=self.output_root,
            project_id="project-cache",
            d1_input_hash="d1-cache",
            input_hash="b" * 64,
            ratio="9:16",
            config={
                "subtitle": {"enabled": True, "position": "top"},
                "bgm": {
                    "asset_id": None, "volume": 0.18,
                    "fade_in_ms": 500, "fade_out_ms": 800,
                },
            },
            media_plan=_media_plan(),
            shot_inputs={
                "shot-1": [{
                    "id": "line-1", "start_ms": 100, "file": self.voice
                }],
                "shot-2": [],
            },
            runner=fake.runner,
            probe=fake.probe,
            identity_check=lambda: True,
            claim_token="2" * 32,
            claim_check=lambda: True,
            toolchain={"ffmpeg": "fake-1", "ffprobe": "fake-1", "font": "test"},
            master_audio_contract={"master_audio_hash": "c" * 64},
            cached_audio_files=cached,
        )
        self.assertEqual([], fake.calls)
        self.assertEqual(
            "cache_reuse", result["manifest"]["audio"]["loudness_mode"]
        )
        self.assertEqual(
            "c" * 64,
            result["manifest"]["master_audio"]["master_audio_hash"],
        )

    def test_bundle_rejects_same_spec_cache_with_changed_content(self):
        cache = Path(self.tempdir.name) / "tampered-cache"
        (cache / "shots").mkdir(parents=True)
        paths = {
            ("shot_voice", "shot-1"): cache / "shots" / "shot-1.wav",
            ("shot_voice", "shot-2"): cache / "shots" / "shot-2.wav",
            ("dialogue", ""): cache / "dialogue.wav",
            ("master_audio", ""): cache / "master.wav",
        }
        records = {}
        for key, path in paths.items():
            path.write_bytes(("original:" + path.name).encode("utf-8"))
            records[key] = {
                "path": path,
                "file_hash": engine._hash_file(path),
            }
        paths[("master_audio", "")].write_bytes(b"same-spec-different-audio")
        fake = _FakeMedia()
        with self.assertRaises(engine.ReusableAudioCacheError) as raised:
            engine.build_bundle(
                output_root=self.output_root,
                project_id="project-tampered",
                d1_input_hash="d1-tampered",
                input_hash="d" * 64,
                ratio="9:16",
                config={
                    "subtitle": {"enabled": True, "position": "bottom"},
                    "bgm": {
                        "asset_id": None, "volume": 0.18,
                        "fade_in_ms": 500, "fade_out_ms": 800,
                    },
                },
                media_plan=_media_plan(),
                shot_inputs={
                    "shot-1": [{
                        "id": "line-1", "start_ms": 100,
                        "file": self.voice,
                    }],
                    "shot-2": [],
                },
                runner=fake.runner,
                probe=fake.probe,
                identity_check=lambda: True,
                claim_token="3" * 32,
                claim_check=lambda: True,
                toolchain={
                    "ffmpeg": "fake-1", "ffprobe": "fake-1", "font": "test"
                },
                cached_audio_files=records,
            )
        self.assertEqual("audio_cache_hash_mismatch", raised.exception.code)
        self.assertEqual([], fake.calls)

    def test_bundle_rejects_incomplete_audio_cache(self):
        with self.assertRaises(engine.ReusableAudioCacheError):
            engine.build_bundle(
                output_root=self.output_root,
                project_id="project-incomplete",
                d1_input_hash="d1-incomplete",
                input_hash="e" * 64,
                ratio="9:16",
                config={
                    "subtitle": {"enabled": True, "position": "bottom"},
                    "bgm": {
                        "asset_id": None, "volume": 0.18,
                        "fade_in_ms": 500, "fade_out_ms": 800,
                    },
                },
                media_plan=_media_plan(),
                shot_inputs={"shot-1": [], "shot-2": []},
                runner=_FakeMedia().runner,
                probe=_FakeMedia.probe,
                identity_check=lambda: True,
                claim_token="4" * 32,
                claim_check=lambda: True,
                toolchain={
                    "ffmpeg": "fake-1", "ffprobe": "fake-1", "font": "test"
                },
                cached_audio_files={
                    ("master_audio", ""): {
                        "path": self.voice,
                        "file_hash": engine._hash_file(self.voice),
                    }
                },
            )

    def test_bundle_with_bgm_runs_loop_and_ducking(self):
        fake = _FakeMedia()
        bgm = Path(self.tempdir.name) / "bgm.mp3"
        bgm.write_bytes(b"bgm")
        result = engine.build_bundle(
            output_root=self.output_root,
            project_id="project-bgm",
            d1_input_hash="d1-hash",
            input_hash="b" * 64,
            ratio="16:9",
            config={
                "subtitle": {"enabled": False, "position": "bottom"},
                "bgm": {
                    "asset_id": 3, "volume": 0.2,
                    "fade_in_ms": 500, "fade_out_ms": 800,
                },
            },
            media_plan=_media_plan(),
            shot_inputs={
                "shot-1": [{
                    "id": "line-1", "start_ms": 100, "file": self.voice,
                }],
                "shot-2": [],
            },
            bgm_source=bgm,
            runner=fake.runner,
            probe=fake.probe,
            identity_check=lambda: True,
            claim_token="2" * 32,
            claim_check=lambda: True,
            toolchain={"ffmpeg": "fake", "ffprobe": "fake", "font": "test"},
        )
        self.assertIn("bgm", {item["kind"] for item in result["artifacts"]})
        flattened = "\n".join(" ".join(call) for call in fake.calls)
        self.assertIn("-stream_loop -1", flattened)
        self.assertIn("sidechaincompress", flattened)
        ass = self.output_root / result["directory"] / "subtitles.ass"
        self.assertNotIn("Dialogue:", ass.read_text(encoding="utf-8"))

    def test_fully_silent_bundle_bypasses_loudnorm(self):
        fake = _FakeMedia()
        result = engine.build_bundle(
            output_root=self.output_root,
            project_id="project-silent",
            d1_input_hash="d1-hash",
            input_hash="e" * 64,
            ratio="16:9",
            config={
                "subtitle": {"enabled": True, "position": "bottom"},
                "bgm": {
                    "asset_id": None, "volume": 0.18,
                    "fade_in_ms": 500, "fade_out_ms": 800,
                },
            },
            media_plan=_silent_media_plan(),
            shot_inputs={"shot-1": [], "shot-2": []},
            runner=fake.runner,
            probe=fake.probe,
            identity_check=lambda: True,
            claim_token="3" * 32,
            claim_check=lambda: True,
            toolchain={"ffmpeg": "fake", "ffprobe": "fake", "font": "test"},
        )
        flattened = "\n".join(" ".join(call) for call in fake.calls)
        self.assertNotIn("loudnorm", flattened)
        manifest = result["manifest"]
        self.assertEqual("silence_bypass",
                         manifest["audio"]["loudness_mode"])
        self.assertIsNone(manifest["audio"]["loudness_measurements"])

    def test_changed_identity_discards_temporary_outputs(self):
        fake = _FakeMedia()
        checks = iter([True, False])
        with self.assertRaises(engine.BundleBuildError) as raised:
            engine.build_bundle(
                output_root=self.output_root,
                project_id="project-change",
                d1_input_hash="d1-hash",
                input_hash="c" * 64,
                ratio="9:16",
                config={
                    "subtitle": {"enabled": True, "position": "bottom"},
                    "bgm": {
                        "asset_id": None, "volume": 0.18,
                        "fade_in_ms": 500, "fade_out_ms": 800,
                    },
                },
                media_plan=_media_plan(),
                shot_inputs={
                    "shot-1": [{
                        "id": "line-1", "start_ms": 100, "file": self.voice,
                    }],
                    "shot-2": [],
                },
                runner=fake.runner,
                probe=fake.probe,
                identity_check=lambda: next(checks),
                claim_token="4" * 32,
                claim_check=lambda: True,
                toolchain={"ffmpeg": "fake", "ffprobe": "fake", "font": "test"},
            )
        self.assertEqual(
            "source_changed_during_audio_build", raised.exception.code
        )
        self.assertFalse(
            (self.output_root / "short_drama_assembly" /
             "project-change" / ("c" * 64)).exists()
        )
        temp_items = list(
            (self.output_root / "short_drama_assembly").glob(".tmp-*")
        )
        self.assertEqual([], temp_items)

    def test_invalid_existing_target_is_quarantined_before_rebuild(self):
        target = (
            self.output_root / "short_drama_assembly" /
            "project-fail" / ("d" * 64)
        )
        target.mkdir(parents=True)
        sentinel = target / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        fake = _FakeMedia(fail_after=1)
        with self.assertRaises(engine.BundleBuildError) as raised:
            engine.build_bundle(
                output_root=self.output_root,
                project_id="project-fail",
                d1_input_hash="d1-hash",
                input_hash="d" * 64,
                ratio="9:16",
                config={
                    "subtitle": {"enabled": True, "position": "bottom"},
                    "bgm": {
                        "asset_id": None, "volume": 0.18,
                        "fade_in_ms": 500, "fade_out_ms": 800,
                    },
                },
                media_plan=_media_plan(),
                shot_inputs={
                    "shot-1": [{
                        "id": "line-1", "start_ms": 100, "file": self.voice,
                    }],
                    "shot-2": [],
                },
                runner=fake.runner,
                probe=fake.probe,
                identity_check=lambda: True,
                claim_token="5" * 32,
                claim_check=lambda: True,
                toolchain={"ffmpeg": "fake", "ffprobe": "fake", "font": "test"},
            )
        self.assertEqual("audio_mix_failed", raised.exception.code)
        self.assertFalse(target.exists())
        quarantine = target.parent / (
            f".stale-{'d' * 64}-{'5' * 32}"
        )
        self.assertEqual(
            "keep",
            (quarantine / "keep.txt").read_text(encoding="utf-8"),
        )

    def test_complete_existing_target_is_recovered_without_rendering(self):
        first_media = _FakeMedia()
        arguments = {
            "output_root": self.output_root,
            "project_id": "project-recover",
            "d1_input_hash": "d1-hash",
            "input_hash": "f" * 64,
            "ratio": "9:16",
            "config": {
                "subtitle": {"enabled": True, "position": "bottom"},
                "bgm": {
                    "asset_id": None, "volume": 0.18,
                    "fade_in_ms": 500, "fade_out_ms": 800,
                },
            },
            "media_plan": _media_plan(),
            "shot_inputs": {
                "shot-1": [{
                    "id": "line-1", "start_ms": 100, "file": self.voice,
                }],
                "shot-2": [],
            },
            "probe": first_media.probe,
            "identity_check": lambda: True,
            "claim_check": lambda: True,
            "toolchain": {
                "ffmpeg": "fake", "ffprobe": "fake", "font": "test",
            },
        }
        first = engine.build_bundle(
            **arguments, runner=first_media.runner, claim_token="6" * 32
        )
        second_media = _FakeMedia(fail_after=1)
        recovered = engine.build_bundle(
            **arguments, runner=second_media.runner, claim_token="7" * 32
        )
        self.assertFalse(first["recovered"])
        self.assertTrue(recovered["recovered"])
        self.assertEqual([], second_media.calls)
        self.assertEqual(
            {item["file_hash"] for item in first["artifacts"]},
            {item["file_hash"] for item in recovered["artifacts"]},
        )

    def test_corrupt_existing_bundle_is_quarantined_and_rebuilt(self):
        first_media = _FakeMedia()
        arguments = {
            "output_root": self.output_root,
            "project_id": "project-rebuild",
            "d1_input_hash": "d1-hash",
            "input_hash": "9" * 64,
            "ratio": "9:16",
            "config": {
                "subtitle": {"enabled": True, "position": "bottom"},
                "bgm": {
                    "asset_id": None, "volume": 0.18,
                    "fade_in_ms": 500, "fade_out_ms": 800,
                },
            },
            "media_plan": _media_plan(),
            "shot_inputs": {
                "shot-1": [{
                    "id": "line-1", "start_ms": 100, "file": self.voice,
                }],
                "shot-2": [],
            },
            "probe": first_media.probe,
            "identity_check": lambda: True,
            "claim_check": lambda: True,
            "toolchain": {
                "ffmpeg": "fake", "ffprobe": "fake", "font": "test",
            },
        }
        first = engine.build_bundle(
            **arguments, runner=first_media.runner, claim_token="9" * 32
        )
        target = self.output_root / first["directory"]
        (target / "master.wav").write_bytes(b"corrupt")

        second_media = _FakeMedia()
        rebuilt = engine.build_bundle(
            **arguments, runner=second_media.runner, claim_token="a" * 32
        )
        self.assertFalse(rebuilt["recovered"])
        self.assertTrue(second_media.calls)
        quarantine = self.output_root / rebuilt["quarantined_directory"]
        self.assertEqual(b"corrupt", (quarantine / "master.wav").read_bytes())
        self.assertNotEqual(b"corrupt", (target / "master.wav").read_bytes())

    def test_lost_claim_cannot_touch_existing_target(self):
        target = (
            self.output_root / "short_drama_assembly" /
            "project-claim" / ("8" * 64)
        )
        target.mkdir(parents=True)
        sentinel = target / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(engine.BundleBuildError) as raised:
            engine.build_bundle(
                output_root=self.output_root,
                project_id="project-claim",
                d1_input_hash="d1-hash",
                input_hash="8" * 64,
                ratio="9:16",
                config={},
                media_plan=_media_plan(),
                shot_inputs={
                    "shot-1": [{
                        "id": "line-1", "start_ms": 100, "file": self.voice,
                    }],
                    "shot-2": [],
                },
                runner=_FakeMedia().runner,
                probe=_FakeMedia.probe,
                identity_check=lambda: True,
                claim_token="8" * 32,
                claim_check=lambda: False,
                toolchain={
                    "ffmpeg": "fake", "ffprobe": "fake", "font": "test",
                },
            )
        self.assertEqual("build_claim_lost", raised.exception.code)
        self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
