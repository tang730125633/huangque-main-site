import json
import pathlib
import shutil
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_HTML = ROOT / "site" / "workbench" / "script.html"


def _extract_function(source, name):
    marker = f"function {name}("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


class ReversePromptHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node is required for reverse prompt history contracts")
        cls.html = SCRIPT_HTML.read_text(encoding="utf-8")
        cls.functions = "\n".join(
            _extract_function(cls.html, name)
            for name in (
                "isReversePromptErrorText",
                "validReversePromptText",
                "reverseResultPrompt",
                "reverseLegacyPrompt",
                "breakdownMetaFromResult",
                "isBreakdownHistoryMeta",
            )
        )

    def _normalize(self, payload):
        harness = f"""
function normalizeBreakdownScenes(value){{return value||[];}}
{self.functions}
var meta=breakdownMetaFromResult({json.dumps(payload, ensure_ascii=False)});
process.stdout.write(JSON.stringify({{meta:meta,valid:isBreakdownHistoryMeta(meta)}}));
"""
        result = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(result.stdout)

    def test_history_preserves_reverse_timeline_scores_and_evidence_mapping(self):
        payload = {
            "type": "breakdown_reverse",
            "prompt": "RESTORABLE PROMPT",
            "frame_thumbnails": [f"frame-{index}" for index in range(1, 10)],
            "reference_thumbnail_indices": [2, 4, 6, 8],
            "audit_thumbnail_indices": [1, 3, 5, 7],
            "frame_manifest": [{"global_frame_number": 1}],
            "timeline_audit": {"precision_seconds": 0.1, "windows": [[0, 4, "0-4"]]},
            "quality_score": {"total": 95, "components": {}},
            "reverse_audit": {"segments": [{"segment_id": 1}]},
        }
        result = self._normalize(payload)
        self.assertTrue(result["valid"])
        meta = result["meta"]
        self.assertEqual(meta["prompt"], "RESTORABLE PROMPT")
        self.assertEqual(meta["frame_thumbnails"], payload["frame_thumbnails"][:8])
        self.assertEqual(meta["reference_thumbnail_indices"], [2, 4, 6, 8])
        self.assertEqual(meta["audit_thumbnail_indices"], [1, 3, 5, 7])
        self.assertEqual(meta["timeline_audit"]["precision_seconds"], 0.1)
        self.assertEqual(meta["quality_score"]["total"], 95)
        self.assertEqual(meta["reverse_audit"]["segments"][0]["segment_id"], 1)

    def test_invalid_failure_result_is_not_saved_as_history(self):
        result = self._normalize(
            {
                "type": "breakdown_reverse",
                "prompt": "反推失败：上游异常 · 已退点",
            }
        )
        self.assertFalse(result["valid"])

    def test_history_restore_passes_all_reverse_metadata_back_to_renderer(self):
        for field in (
            "sections:m.sections||null",
            "reference_thumbnail_indices:m.reference_thumbnail_indices||[]",
            "audit_thumbnail_indices:m.audit_thumbnail_indices||[]",
            "timeline_audit:m.timeline_audit||null",
            "quality_score:m.quality_score||null",
            "reverse_audit:m.reverse_audit||null",
        ):
            self.assertIn(field, self.html)


if __name__ == "__main__":
    unittest.main()
