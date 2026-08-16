import json
import math
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace


import sys

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import short_drama_assembly_audio as audio
from content_domains import short_drama_assembly_plan as media_plan


class ShortDramaAssemblyAudioTests(unittest.TestCase):
    def test_silent_shot_has_exact_pcm_contract(self):
        command = audio.build_shot_voice_command([], 5000, Path("shot.wav"))
        self.assertEqual("ffmpeg", command[0])
        self.assertIn("anullsrc=r=48000:cl=stereo:d=5.000", command)
        self.assertIn("pcm_s16le", command)
        self.assertEqual("shot.wav", command[-1])

    def test_voice_lines_are_sorted_delayed_and_mixed_without_shell(self):
        lines = [
            {"id": "b", "start_ms": 1500, "file": Path("b.wav")},
            {"id": "a", "start_ms": 100, "file": Path("a.wav")},
        ]
        command = audio.build_shot_voice_command(
            lines, 5000, Path("shot.wav")
        )
        self.assertLess(command.index("a.wav"), command.index("b.wav"))
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("adelay=100|100", graph)
        self.assertIn("adelay=1500|1500", graph)
        self.assertIn("amix=inputs=3:duration=first", graph)
        self.assertIn("atrim=duration=5.000", graph)
        self.assertNotIn("shell", command)

    def test_concat_command_preserves_shot_order_and_duration(self):
        command = audio.build_dialogue_concat_command(
            [Path("s2.wav"), Path("s1.wav")],
            10000,
            Path("dialogue.wav"),
        )
        self.assertLess(command.index("s2.wav"), command.index("s1.wav"))
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("concat=n=2:v=0:a=1", graph)
        self.assertIn("atrim=duration=10.000", graph)

    def test_bgm_command_loops_trims_fades_and_clamps_volume(self):
        command = audio.build_bgm_command(
            Path("bgm.mp3"),
            30000,
            {
                "volume": 0.18,
                "fade_in_ms": 500,
                "fade_out_ms": 800,
            },
            Path("bgm.wav"),
        )
        self.assertIn("-stream_loop", command)
        self.assertIn("-1", command)
        graph = command[command.index("-af") + 1]
        self.assertIn("volume=0.180000", graph)
        self.assertIn("afade=t=in:st=0:d=0.500", graph)
        self.assertIn("afade=t=out:st=29.200:d=0.800", graph)
        self.assertIn("atrim=duration=30.000", graph)

        with self.assertRaises(audio.AudioEngineError) as raised:
            audio.build_bgm_command(
                Path("bgm.mp3"), 30000,
                {"volume": 2, "fade_in_ms": 0, "fade_out_ms": 0},
                Path("bgm.wav"),
            )
        self.assertEqual("bgm_probe_failed", raised.exception.code)

    def test_loudness_analysis_and_master_commands_include_ducking(self):
        analysis = audio.build_loudness_analysis_command(
            Path("dialogue.wav"), Path("bgm.wav"), 30000
        )
        graph = analysis[analysis.index("-filter_complex") + 1]
        self.assertIn("sidechaincompress", graph)
        self.assertIn("loudnorm=I=-16.0:TP=-1.5:LRA=11.0:print_format=json", graph)
        self.assertEqual(
            "info", analysis[analysis.index("-loglevel") + 1]
        )
        self.assertEqual("-", analysis[-1])

        measured = {
            "input_i": "-20.1",
            "input_tp": "-3.0",
            "input_lra": "5.2",
            "input_thresh": "-30.0",
            "target_offset": "0.3",
        }
        render = audio.build_master_command(
            Path("dialogue.wav"), Path("bgm.wav"), 30000,
            measured, Path("master.wav"),
        )
        graph = render[render.index("-filter_complex") + 1]
        self.assertIn("measured_I=-20.1", graph)
        self.assertIn("offset=0.3", graph)
        self.assertIn("linear=true", graph)
        self.assertGreater(
            graph.index("aresample=48000"),
            graph.index("loudnorm="),
        )
        self.assertIn(
            "aformat=sample_fmts=s16:channel_layouts=stereo",
            graph,
        )
        self.assertIn("pcm_s16le", render)
        self.assertEqual("48000", render[render.index("-ar") + 1])
        self.assertEqual("2", render[render.index("-ac") + 1])
        self.assertEqual(
            "error", render[render.index("-loglevel") + 1]
        )

    def test_soundscape_places_manual_cues_and_three_track_mix_ducks_them(self):
        soundscape = audio.build_soundscape_command(
            [{
                "file": Path("door.wav"),
                "timeline_start_ms": 1200,
                "timeline_end_ms": 2600,
                "loop": False,
                "volume": 0.55,
                "fade_in_ms": 100,
                "fade_out_ms": 200,
            }],
            5000,
            Path("soundscape.wav"),
        )
        graph = soundscape[soundscape.index("-filter_complex") + 1]
        self.assertIn("adelay=1200|1200", graph)
        self.assertIn("volume=0.550000", graph)
        self.assertIn("afade=t=out:st=1.200:d=0.200", graph)
        self.assertIn("amix=inputs=2:duration=first", graph)

        analysis = audio.build_loudness_analysis_command(
            Path("dialogue.wav"), Path("bgm.wav"), 5000,
            Path("soundscape.wav"),
        )
        mix = analysis[analysis.index("-filter_complex") + 1]
        self.assertEqual(3, analysis.count("-i"))
        self.assertIn("[1:a][side_bgm]sidechaincompress", mix)
        self.assertIn("[2:a][side_sfx]sidechaincompress", mix)
        self.assertIn("amix=inputs=3:duration=first", mix)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is unavailable")
    def test_real_loudness_analysis_emits_parseable_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "tone.wav"
            with wave.open(str(source), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(audio.SAMPLE_RATE)
                frames = bytearray()
                for index in range(audio.SAMPLE_RATE):
                    sample = int(
                        8000 * math.sin(2 * math.pi * 440 * index / audio.SAMPLE_RATE)
                    )
                    frames.extend(struct.pack("<hh", sample, sample))
                output.writeframes(frames)

            result = audio.run_ffmpeg(
                audio.build_loudness_analysis_command(
                    source, None, 1000
                )
            )
            measured = audio.parse_loudnorm(result.stderr)

        self.assertEqual(
            {
                "input_i",
                "input_tp",
                "input_lra",
                "input_thresh",
                "target_offset",
            },
            set(measured),
        )

    def test_no_bgm_master_uses_dialogue_only(self):
        command = audio.build_master_command(
            Path("dialogue.wav"), None, 5000,
            {
                "input_i": "-16.0", "input_tp": "-2.0",
                "input_lra": "3.0", "input_thresh": "-26.0",
                "target_offset": "0.0",
            },
            Path("master.wav"),
        )
        self.assertNotIn("sidechaincompress", command[-4])
        self.assertEqual(1, command.count("-i"))

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg/ffprobe are unavailable",
    )
    def test_real_master_is_48khz_stereo_after_loudnorm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "tone.wav"
            master = root / "master.wav"
            with wave.open(str(source), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(44100)
                frames = bytearray()
                for index in range(44100):
                    sample = int(
                        8000 * math.sin(2 * math.pi * 440 * index / 44100)
                    )
                    frames.extend(struct.pack("<h", sample))
                output.writeframes(frames)

            analysis = audio.run_ffmpeg(
                audio.build_loudness_analysis_command(source, None, 1000)
            )
            measured = audio.parse_loudnorm(analysis.stderr)
            audio.run_ffmpeg(
                audio.build_master_command(
                    source, None, 1000, measured, master
                )
            )
            probe = media_plan.probe_media(master)

        self.assertEqual(48000, probe["audio"]["sample_rate"])
        self.assertEqual(2, probe["audio"]["channels"])
        audio.validate_audio_probe(probe, 1000)

    def test_silent_master_bypasses_loudnorm_and_keeps_pcm_contract(self):
        command = audio.build_silent_master_command(
            Path("dialogue.wav"), 5000, Path("master.wav")
        )
        self.assertEqual(1, command.count("-i"))
        self.assertNotIn("loudnorm", " ".join(command))
        self.assertIn("aresample=48000", command[command.index("-af") + 1])
        self.assertIn("channel_layouts=stereo", command[command.index("-af") + 1])
        self.assertIn("atrim=duration=5.000", command[command.index("-af") + 1])
        self.assertIn("pcm_s16le", command)
        self.assertEqual("48000", command[command.index("-ar") + 1])
        self.assertEqual("2", command[command.index("-ac") + 1])

    def test_parse_loudnorm_rejects_missing_or_non_finite_values(self):
        payload = {
            "input_i": "-19.2", "input_tp": "-2.3", "input_lra": "4.1",
            "input_thresh": "-29.4", "target_offset": "0.2",
        }
        result = audio.parse_loudnorm("log\n" + json.dumps(payload) + "\n")
        self.assertEqual(payload, result)
        for invalid in ("no json", '{"input_i":"-inf"}'):
            with self.assertRaises(audio.AudioEngineError) as raised:
                audio.parse_loudnorm(invalid)
            self.assertEqual("loudness_analysis_failed", raised.exception.code)

    def test_runner_maps_unavailable_timeout_and_process_failure(self):
        def missing(_args, **_kwargs):
            raise FileNotFoundError()

        with self.assertRaises(audio.AudioEngineError) as raised:
            audio.run_ffmpeg(["ffmpeg", "-version"], runner=missing)
        self.assertEqual("ffmpeg_unavailable", raised.exception.code)

        def timeout(_args, **_kwargs):
            raise subprocess.TimeoutExpired("ffmpeg", 1)

        with self.assertRaises(audio.AudioEngineError) as raised:
            audio.run_ffmpeg(["ffmpeg", "-version"], runner=timeout)
        self.assertEqual("audio_mix_failed", raised.exception.code)

        def failed(_args, **_kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="secret path")

        with self.assertRaises(audio.AudioEngineError) as raised:
            audio.run_ffmpeg(["ffmpeg", "-version"], runner=failed)
        self.assertEqual("audio_mix_failed", raised.exception.code)
        self.assertNotIn("secret path", str(raised.exception))

    def test_audio_probe_contract(self):
        valid = {
            "duration_ms": 5000,
            "video": None,
            "audio": {"sample_rate": 48000, "channels": 2},
        }
        audio.validate_audio_probe(valid, 5000)
        for probe, code in (
            ({"duration_ms": 5000, "video": None, "audio": None},
             "audio_stream_missing"),
            ({**valid, "duration_ms": 5100}, "audio_duration_mismatch"),
            ({**valid, "audio": {"sample_rate": 44100, "channels": 2}},
             "audio_mix_failed"),
        ):
            with self.assertRaises(audio.AudioEngineError) as raised:
                audio.validate_audio_probe(probe, 5000)
            self.assertEqual(code, raised.exception.code)

    def test_ffmpeg_version_gate(self):
        def supported(_args, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout="ffmpeg version 6.1 Copyright\n",
                stderr="",
            )

        self.assertEqual(
            "ffmpeg version 6.1 Copyright",
            audio.inspect_ffmpeg(runner=supported),
        )

        def old(_args, **_kwargs):
            return SimpleNamespace(
                returncode=0, stdout="ffmpeg version 3.4\n", stderr=""
            )

        with self.assertRaises(audio.AudioEngineError) as raised:
            audio.inspect_ffmpeg(runner=old)
        self.assertEqual("ffmpeg_version_unsupported", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
