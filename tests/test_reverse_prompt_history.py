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
                "reverseAuditData",
                "reverseReferenceThumbnailIndices",
                "reverseReferenceImages",
                "breakdownMetaFromResult",
                "compactBreakdownHistoryMeta",
                "isBreakdownHistoryMeta",
                "saveBreakdownHistory",
                "loadBreakdownHistoryDetail",
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

    def test_generation_uses_reference_indices_not_audit_frames(self):
        harness = f"""
{self.functions}
var payload={{
  frame_thumbnails:['audit-1','ref-2','audit-3','ref-4','audit-5','ref-6','audit-7','ref-8'],
  reference_thumbnail_indices:[2,4,6,8]
}};
process.stdout.write(JSON.stringify(reverseReferenceImages(payload)));
"""
        result = subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True,
            text=True, encoding="utf-8",
        )
        self.assertEqual(
            ["ref-2", "ref-4", "ref-6", "ref-8"],
            json.loads(result.stdout),
        )

    def test_legacy_history_without_mapping_keeps_first_four_frames(self):
        harness = f"""
{self.functions}
process.stdout.write(JSON.stringify(reverseReferenceImages({{
  frame_thumbnails:['f1','f2','f3','f4','f5']
}})));
"""
        result = subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True,
            text=True, encoding="utf-8",
        )
        self.assertEqual(["f1", "f2", "f3", "f4"], json.loads(result.stdout))

    def test_history_restore_passes_all_reverse_metadata_back_to_renderer(self):
        self.assertIn("loadBreakdownHistoryDetail(item).then(function(detail)", self.html)
        self.assertIn("renderBreakdownReverse(Object.assign({},detail", self.html)
        self.assertIn("'/api/gen/job/'+encodeURIComponent(jobId)", self.html)
        self.assertIn("headers:{'Authorization':'Bearer '+tok(),'Cache-Control':'no-cache'}", self.html)

    def _save_large_history(self, force_failure=False):
        harness = f"""
var BREAKDOWN_HISTORY_KEY='history';
var storage={{}};
var toasts=[];
var historyState={{textContent:''}};
var localStorage={{
  getItem:function(key){{return storage[key]||null;}},
  setItem:function(key,value){{
    if({str(force_failure).lower()}) throw new Error('QuotaExceededError');
    if(value.length>5*1024*1024) throw new Error('QuotaExceededError');
    storage[key]=value;
  }}
}};
var window={{HQ:{{toast:function(message){{toasts.push(message);}}}}}};
var HQ=window.HQ;
function normalizeBreakdownScenes(value){{return value||[];}}
{self.functions}
var large='data:image/jpeg;base64,'+'A'.repeat(320*1024);
var payload={{
  type:'breakdown_reverse',_history_job_id:3258,
  source_url:'https://example.invalid/video',source_title:'大缩略图反推',
  prompt:'可恢复提示词',frame_thumbnails:Array(8).fill(large),
  reference_thumbnail_indices:[2,4,6,8],audit_thumbnail_indices:[1,3,5,7],
  frame_manifest:[{{global_frame_number:1}}],
  timeline_audit:{{precision_seconds:0.1}},quality_score:{{total:96}},
  reverse_audit:{{segments:[{{segment_id:1}}]}}
}};
var ok=saveBreakdownHistory(payload);
var raw=storage[BREAKDOWN_HISTORY_KEY]||'';
var saved=raw?JSON.parse(raw)[0]:null;
process.stdout.write(JSON.stringify({{
  ok:ok,length:raw.length,toasts:toasts,state:historyState.textContent,
  jobId:saved&&saved.job_id,meta:saved&&saved.meta
}}));
"""
        result = subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True,
            text=True, encoding="utf-8",
        )
        return json.loads(result.stdout)

    def test_local_history_is_compact_with_eight_realistic_thumbnails(self):
        got = self._save_large_history()
        self.assertTrue(got["ok"])
        self.assertLess(got["length"], 16 * 1024)
        self.assertEqual(got["jobId"], 3258)
        self.assertEqual(got["meta"]["job_id"], 3258)
        for large_field in (
            "frame_thumbnails", "frame_manifest", "timeline_audit",
            "quality_score", "reverse_audit", "sections",
        ):
            self.assertNotIn(large_field, got["meta"])

    def test_local_history_quota_failure_is_visible(self):
        got = self._save_large_history(force_failure=True)
        self.assertFalse(got["ok"])
        self.assertEqual(got["toasts"], ["本机历史保存失败，完整结果仍保存在服务器"])
        self.assertIn("服务器生成记录恢复", got["state"])

    def test_owned_job_detail_restores_complete_server_result(self):
        harness = f"""
var loginCalls=0;
var window={{HQ:{{login:function(){{loginCalls++;}}}}}};
var HQ=window.HQ;
function tok(){{return 'cookie';}}
var full={{
  type:'breakdown_reverse',prompt:'服务端完整提示词',
  frame_thumbnails:['f1','f2','f3','f4','f5','f6','f7','f8'],
  reference_thumbnail_indices:[2,4,6,8],
  audit_thumbnail_indices:[1,3,5,7],timeline_audit:{{precision_seconds:0.1}},
  quality_score:{{total:97}},reverse_audit:{{segments:[{{segment_id:1}}]}}
}};
var requested='';
function fetch(url,options){{
  requested=url;
  return Promise.resolve({{ok:true,status:200,json:function(){{return Promise.resolve({{result:full}});}}}});
}}
{self.functions}
loadBreakdownHistoryDetail({{job_id:3258,meta:{{type:'breakdown_reverse'}}}}).then(function(detail){{
  process.stdout.write(JSON.stringify({{requested:requested,detail:detail,loginCalls:loginCalls}}));
}});
"""
        result = subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True,
            text=True, encoding="utf-8",
        )
        got = json.loads(result.stdout)
        self.assertEqual(got["requested"], "/api/gen/job/3258")
        self.assertEqual(len(got["detail"]["frame_thumbnails"]), 8)
        self.assertEqual(got["detail"]["reference_thumbnail_indices"], [2, 4, 6, 8])
        self.assertEqual(got["detail"]["quality_score"]["total"], 97)
        self.assertEqual(got["detail"]["_history_job_id"], 3258)
        self.assertEqual(got["loginCalls"], 0)


if __name__ == "__main__":
    unittest.main()
