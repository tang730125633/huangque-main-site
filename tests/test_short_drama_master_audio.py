import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import short_drama_master_audio as master_audio


def plan():
    return {
        "project_duration_ms": 10000,
        "shots": [
            {
                "id": "shot-1", "start_ms": 0, "end_ms": 5000,
                "duration_ms": 5000,
                "audio": {"lines": [
                    {
                        "id": "line-1", "start_ms": 100, "end_ms": 900,
                        "audio_duration_ms": 800,
                        "subtitle_visible": True,
                        "subtitle_text": "第一句",
                    }
                ]},
            },
            {
                "id": "shot-2", "start_ms": 5000, "end_ms": 10000,
                "duration_ms": 5000, "audio": {"lines": []},
            },
        ],
    }


def sources():
    return [{
        "shot_id": "shot-1", "line_id": "line-1", "version": 2,
        "sha256": "a" * 64, "size": 1024,
    }]


class ShortDramaMasterAudioTests(unittest.TestCase):
    def test_contract_is_stable_and_has_per_shot_hashes(self):
        first = master_audio.build_contract(plan(), sources(), None, {})
        second = master_audio.build_contract(plan(), sources(), None, {})
        self.assertEqual(first, second)
        self.assertEqual(64, len(first["master_audio_hash"]))
        self.assertEqual(2, len(first["shots"]))
        self.assertTrue(all(
            len(item["shot_audio_hash"]) == 64 for item in first["shots"]
        ))
        self.assertEqual(48000, first["sample_rate"])
        self.assertEqual(2, first["channels"])
        self.assertEqual("pcm_s16le", first["codec"])

    def test_bgm_or_voice_change_invalidates_master_hash(self):
        baseline = master_audio.build_contract(plan(), sources(), None, {})
        changed_sources = sources()
        changed_sources[0]["sha256"] = "b" * 64
        voice_changed = master_audio.build_contract(
            plan(), changed_sources, None, {}
        )
        bgm_changed = master_audio.build_contract(
            plan(), sources(),
            {"id": "bgm-1", "sha256": "c" * 64, "size": 2000},
            {"volume": 0.2, "fade_in_ms": 500, "fade_out_ms": 800},
        )
        self.assertNotEqual(
            baseline["master_audio_hash"], voice_changed["master_audio_hash"]
        )
        self.assertNotEqual(
            baseline["master_audio_hash"], bgm_changed["master_audio_hash"]
        )

    def test_subtitle_presentation_is_not_part_of_audio_identity(self):
        first = master_audio.build_contract(plan(), sources(), None, {})
        subtitle_changed = plan()
        line = subtitle_changed["shots"][0]["audio"]["lines"][0]
        line["end_ms"] = 4900
        line["subtitle_visible"] = False
        line["subtitle_text"] = "完全不同的隐藏字幕"
        second = master_audio.build_contract(
            subtitle_changed, sources(), None, {}
        )
        self.assertEqual(
            first["master_audio_hash"], second["master_audio_hash"]
        )

    def test_audio_duration_changes_hash_and_not_subtitle_end(self):
        baseline = master_audio.build_contract(plan(), sources(), None, {})
        changed = plan()
        changed["shots"][0]["audio"]["lines"][0][
            "audio_duration_ms"
        ] = 801
        updated = master_audio.build_contract(changed, sources(), None, {})
        self.assertNotEqual(
            baseline["master_audio_hash"], updated["master_audio_hash"]
        )

    def test_rejects_gap_audio_overlap_and_audio_out_of_bounds(self):
        invalid = plan()
        invalid["shots"][1]["start_ms"] = 4999
        with self.assertRaises(master_audio.MasterAudioContractError):
            master_audio.build_contract(invalid, sources(), None, {})
        invalid = plan()
        invalid["shots"][0]["audio"]["lines"][0][
            "audio_duration_ms"
        ] = 5001
        with self.assertRaises(master_audio.MasterAudioContractError):
            master_audio.build_contract(invalid, sources(), None, {})
        overlapping = plan()
        overlapping["shots"][0]["audio"]["lines"].append({
            "id": "line-2",
            "start_ms": 899,
            "end_ms": 1000,
            "audio_duration_ms": 100,
        })
        overlapping_sources = sources() + [{
            "shot_id": "shot-1", "line_id": "line-2", "version": 1,
            "sha256": "b" * 64, "size": 100,
        }]
        with self.assertRaises(master_audio.MasterAudioContractError):
            master_audio.build_contract(
                overlapping, overlapping_sources, None, {}
            )

    def test_hidden_subtitle_overlap_does_not_create_audio_overlap(self):
        value = plan()
        value["shots"][0]["audio"]["lines"][0]["end_ms"] = 4900
        value["shots"][0]["audio"]["lines"].append({
            "id": "line-2",
            "start_ms": 1000,
            "end_ms": 4500,
            "audio_duration_ms": 500,
            "subtitle_visible": False,
            "subtitle_text": "隐藏字幕",
        })
        value_sources = sources() + [{
            "shot_id": "shot-1", "line_id": "line-2", "version": 1,
            "sha256": "b" * 64, "size": 100,
        }]
        contract = master_audio.build_contract(
            value, value_sources, None, {}
        )
        self.assertEqual(2, len(contract["shots"][0]["lines"]))

    def test_snapshot_requires_master_artifact_for_ready_cache(self):
        contract = master_audio.build_contract(plan(), sources(), None, {})
        stale = master_audio.build_snapshot(
            contract, {"status": "ready", "artifacts": []}
        )
        self.assertEqual("stale", stale["status"])
        ready = master_audio.build_snapshot(contract, {
            "status": "ready",
            "artifacts": [{
                "kind": "master_audio", "file_hash": "d" * 64,
                "duration_ms": 10000, "sample_rate": 48000, "channels": 2,
            }],
        })
        self.assertTrue(ready["cache_hit"])
        self.assertEqual(
            contract["master_audio_hash"],
            ready["timeline"]["master_audio_hash"],
        )


if __name__ == "__main__":
    unittest.main()
