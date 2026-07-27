import json
import tempfile
import unittest
from pathlib import Path

from tools.lipsync_poc.adapters import MockLipsyncProvider
from tools.lipsync_poc.manifest import PocSample
from tools.lipsync_poc.redaction import redact
from tools.lipsync_poc.runner import PocRunner
from tools.lipsync_poc.run_poc import main


def fake_probe(path):
    path = Path(path)
    return {
        "duration_ms": 5000,
        "video_stream_count": 1 if path.suffix == ".mp4" else 0,
        "audio_stream_count": 1 if path.suffix == ".wav" else 0,
        "video": {
            "codec": "h264",
            "width": 720,
            "height": 1280,
            "fps": 25,
            "pixel_format": "yuv420p",
        } if path.suffix == ".mp4" else None,
        "audio": {
            "codec": "pcm_s16le",
            "sample_rate": 48000,
            "channels": 1,
            "channel_layout": "mono",
        } if path.suffix == ".wav" else None,
        "format": path.suffix.lstrip("."),
        "size_bytes": path.stat().st_size,
    }


class LipsyncPocRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / "source.mp4"
        self.audio = self.root / "master.wav"
        self.video.write_bytes(b"video")
        self.audio.write_bytes(b"audio")
        self.sample = PocSample(
            sample_id="front-01",
            video_path=self.video,
            audio_path=self.audio,
            transcript="测试对白",
            speaking_mode="visible",
            character_key="host",
            face_target={"type": "character", "value": "host"},
            duration_ms=5000,
            ratio="9:16",
            resolution="720p",
            fps=25,
            tags=("front",),
            notes="",
            input_hash="a" * 64,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_runner_writes_atomic_redacted_report(self):
        output = self.root / "out"
        runner = PocRunner(
            MockLipsyncProvider(),
            probe=fake_probe,
            clock=lambda: 1.0,
        )
        report = runner.run(self.sample, output)
        saved = json.loads(
            (output / "reports/front-01.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report["input_hash"], saved["input_hash"])
        self.assertEqual("pending", saved["human_review"]["review_status"])
        self.assertFalse(
            (output / "reports/front-01.json.part").exists()
        )
        self.assertTrue((output / "media/front-01.mp4").is_file())

    def test_report_does_not_include_absolute_input_paths(self):
        output = self.root / "out"
        report = PocRunner(
            MockLipsyncProvider(),
            probe=fake_probe,
            clock=lambda: 1.0,
        ).run(self.sample, output)
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(str(self.root), encoded)

    def test_redaction_scrubs_secrets_and_url_queries(self):
        result = redact({
            "api_token": "secret-value",
            "authorization": "Bearer secret",
            "result_url": "https://provider.test/result.mp4?token=secret#frag",
            "nested": {"cookie": "session"},
        })
        self.assertEqual("[REDACTED]", result["api_token"])
        self.assertEqual("[REDACTED]", result["authorization"])
        self.assertEqual(
            "https://provider.test/result.mp4",
            result["result_url"],
        )
        self.assertEqual("[REDACTED]", result["nested"]["cookie"])

    def test_redaction_scrubs_tokens_embedded_in_error_messages(self):
        result = redact(
            "request https://provider.test/result?token=secret failed "
            "with Bearer abc123 and api_key=hidden"
        )
        self.assertNotIn("secret", result)
        self.assertNotIn("abc123", result)
        self.assertNotIn("hidden", result)
        self.assertIn("Bearer [REDACTED]", result)

    def test_validate_only_does_not_call_ffprobe_or_provider(self):
        assets = self.root / "assets"
        (assets / "video").mkdir(parents=True)
        (assets / "audio").mkdir()
        (assets / "video/source.mp4").write_bytes(b"video")
        (assets / "audio/master.wav").write_bytes(b"audio")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({
            "manifest_version": "1.0",
            "dataset_name": "test",
            "samples": [{
                "sample_id": "front-01",
                "video_file": "video/source.mp4",
                "audio_file": "audio/master.wav",
                "transcript": "测试",
                "speaking_mode": "visible",
                "character_key": "host",
                "duration_ms": 5000,
                "ratio": "9:16",
                "output_spec": {"resolution": "720p", "fps": 25},
            }],
        }), encoding="utf-8")
        code = main([
            "--manifest", str(manifest),
            "--assets-root", str(assets),
            "--validate-only",
        ])
        self.assertEqual(0, code)


if __name__ == "__main__":
    unittest.main()
