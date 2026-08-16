import sys
import unittest
from pathlib import Path


SERVER = str(Path(__file__).resolve().parents[1] / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from content_domains import short_drama_timeline_rules as rules


def segment(identifier, start, end, character="host", mode="visible"):
    return {
        "id": identifier, "shot_id": "shot-1", "line_id": identifier,
        "character_key": character, "voice_asset_id": "voice-" + identifier,
        "start_ms": start, "end_ms": end, "speaking_mode": mode,
        "face_target": (
            {"type": "character", "value": character}
            if mode == "visible" else None
        ),
    }


class ShortDramaTimelineRuleTests(unittest.TestCase):
    def validate(self, segments, cues=None):
        return rules.validate_timeline(
            5000, [{"shot_id": "shot-1", "start_ms": 0, "end_ms": 5000}],
            {"host", "guest"}, segments, cues or [],
        )

    def test_adjacent_visible_segments_are_allowed(self):
        self.assertEqual([], self.validate([
            segment("a", 0, 1000), segment("b", 1000, 2000, "guest"),
        ]))

    def test_visible_overlap_and_missing_face_target_are_blocked(self):
        second = segment("b", 900, 1800, "guest")
        second["face_target"] = None
        codes = {
            item["code"] for item in self.validate([
                segment("a", 0, 1000), second,
            ])
        }
        self.assertIn("timeline_segment_overlap", codes)
        self.assertIn("timeline_missing_face_target", codes)

    def test_offscreen_and_narration_do_not_require_face_target(self):
        self.assertEqual([], self.validate([
            segment("a", 0, 1000, "host", "offscreen"),
            segment("b", 1000, 2000, "narrator", "narration"),
        ]))

    def test_stale_impact_is_precise(self):
        impact = rules.stale_impact(
            {"alignment_hash": "old", "visual_hash": "same"},
            {"alignment_hash": "new", "visual_hash": "same"},
        )
        self.assertEqual(["alignment_hash"], impact["changed_sources"])
        self.assertEqual(["preview", "subtitles"], impact["downstream"])
