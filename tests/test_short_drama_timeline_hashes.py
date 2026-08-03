import sys
import unittest
from pathlib import Path


SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from content_domains import short_drama_timeline_hashes as hashes


class ShortDramaTimelineHashTests(unittest.TestCase):
    def test_canonical_hash_is_stable_across_input_order(self):
        first = hashes.timeline_hash(5000, [{
            "id": "s1", "shot_id": "shot-1", "line_id": "d1",
            "character_key": "host", "voice_asset_id": "voice-1",
            "start_ms": 100, "end_ms": 800, "speaking_mode": "visible",
            "face_target": {"value": "host", "type": "character"},
        }], [{
            "line_id": "d1", "shot_id": "shot-1", "text": "你好",
            "end_ms": 800, "start_ms": 100,
        }])
        second = hashes.timeline_hash(5000, [{
            "face_target": {"type": "character", "value": "host"},
            "speaking_mode": "visible", "end_ms": 800, "start_ms": 100,
            "voice_asset_id": "voice-1", "character_key": "host",
            "line_id": "d1", "shot_id": "shot-1", "id": "s1",
        }], [{
            "start_ms": 100, "end_ms": 800, "text": "你好",
            "shot_id": "shot-1", "line_id": "d1",
        }])
        self.assertEqual(first, second)
        self.assertEqual(64, len(first))

    def test_subtitle_change_does_not_change_source_hashes_but_changes_timeline(self):
        source = {"master_audio_hash": "a" * 64}
        first = hashes.downstream_input_hash(
            "p1", source, hashes.timeline_hash(1000, [], [{
                "shot_id": "shot", "line_id": "line", "text": "A",
                "start_ms": 0, "end_ms": 500,
            }])
        )
        second = hashes.downstream_input_hash(
            "p1", source, hashes.timeline_hash(1000, [], [{
                "shot_id": "shot", "line_id": "line", "text": "B",
                "start_ms": 0, "end_ms": 500,
            }])
        )
        self.assertNotEqual(first, second)
