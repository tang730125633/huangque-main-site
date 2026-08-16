import json
import tempfile
import unittest
from pathlib import Path

from tools.lipsync_poc import manifest


def valid_document():
    return {
        "manifest_version": "1.0",
        "dataset_name": "baseline",
        "samples": [{
            "sample_id": "front-01",
            "video_file": "video/front.mp4",
            "audio_file": "audio/front.wav",
            "transcript": "今天开始测试。",
            "speaking_mode": "visible",
            "character_key": "host",
            "face_target": {"type": "character", "value": "host"},
            "duration_ms": 5000,
            "ratio": "9:16",
            "output_spec": {"resolution": "720p", "fps": 25},
            "tags": ["front"],
        }],
    }


class LipsyncPocManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "video").mkdir()
        (self.root / "audio").mkdir()
        (self.root / "video" / "front.mp4").write_bytes(b"video")
        (self.root / "audio" / "front.wav").write_bytes(b"audio")
        self.manifest = self.root / "manifest.json"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, document):
        self.manifest.write_text(
            json.dumps(document, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_valid_manifest_resolves_assets_and_builds_request(self):
        self.write(valid_document())
        samples = manifest.load_manifest(self.manifest, self.root)
        self.assertEqual(1, len(samples))
        sample = samples[0]
        self.assertEqual("front-01", sample.sample_id)
        self.assertEqual(64, len(sample.input_hash))
        self.assertEqual("host", sample.to_request().character_key)

    def test_hash_changes_when_audio_changes(self):
        self.write(valid_document())
        before = manifest.load_manifest(self.manifest, self.root)[0].input_hash
        (self.root / "audio" / "front.wav").write_bytes(b"changed-audio")
        after = manifest.load_manifest(self.manifest, self.root)[0].input_hash
        self.assertNotEqual(before, after)

    def test_absolute_path_is_rejected(self):
        document = valid_document()
        document["samples"][0]["video_file"] = str(
            (self.root / "video" / "front.mp4").resolve()
        )
        self.write(document)
        with self.assertRaises(manifest.ManifestError):
            manifest.load_manifest(self.manifest, self.root)

    def test_parent_traversal_is_rejected(self):
        document = valid_document()
        document["samples"][0]["video_file"] = "../outside.mp4"
        self.write(document)
        with self.assertRaises(manifest.ManifestError):
            manifest.load_manifest(self.manifest, self.root)

    def test_unknown_sample_field_is_rejected(self):
        document = valid_document()
        document["samples"][0]["api_token"] = "must-not-be-accepted"
        self.write(document)
        with self.assertRaises(manifest.ManifestError):
            manifest.load_manifest(self.manifest, self.root)

    def test_visible_speech_requires_character(self):
        document = valid_document()
        document["samples"][0].pop("character_key")
        self.write(document)
        with self.assertRaises(manifest.ManifestError):
            manifest.load_manifest(self.manifest, self.root)

    def test_duplicate_sample_id_is_rejected(self):
        document = valid_document()
        document["samples"].append(dict(document["samples"][0]))
        self.write(document)
        with self.assertRaises(manifest.ManifestError):
            manifest.load_manifest(self.manifest, self.root)

    def test_blank_dataset_name_is_rejected(self):
        document = valid_document()
        document["dataset_name"] = " "
        self.write(document)
        with self.assertRaises(manifest.ManifestError):
            manifest.load_manifest(self.manifest, self.root)

    def test_schema_file_is_valid_json(self):
        schema = json.loads(
            (
                Path(__file__).parents[1]
                / "tools/lipsync_poc/sample_manifest.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            "https://json-schema.org/draft/2020-12/schema",
            schema["$schema"],
        )


if __name__ == "__main__":
    unittest.main()
