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


class ReversePromptRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node is required for reverse prompt rendering contracts")
        html = SCRIPT_HTML.read_text(encoding="utf-8")
        cls.functions = "\n".join(
            _extract_function(html, name)
            for name in (
                "isReversePromptErrorText",
                "validReversePromptText",
                "reverseResultPrompt",
                "reverseLegacyPrompt",
                "reverseAuditData",
                "reverseFixedSecond",
                "reverseTransitionText",
                "reverseTimelineSegments",
                "reverseAuditHtml",
                "renderBreakdownReverse",
            )
        )

    def _render(self, payload):
        harness = f"""
{self.functions}
var lastBreakdownReverse=null;
var bdEditing=false;
var bdEditBtn=null;
var bdEditCancelBtn=null;
var meta={{innerHTML:''}};
var scenes={{innerHTML:''}};
var bdResultMeta=null;
var bdSourceLink=null;
function esc(value){{return String(value);}}
function platformText(value){{return value||'';}}
function fmtDur(value){{return value||'';}}
function setBreakdownAnalysis(){{}}
function renderSceneStats(){{}}
function setBreakdownStoryboard(){{}}
function syncModeUi(){{}}
renderBreakdownReverse({json.dumps(payload, ensure_ascii=False)});
process.stdout.write(JSON.stringify({{html:scenes.innerHTML}}));
"""
        result = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(result.stdout)["html"]

    def test_audit_sections_show_prompt_instead_of_seven_empty_cards(self):
        html = self._render(
            {
                "type": "breakdown_reverse",
                "prompt": "REAL REVERSE PROMPT",
                "sections": {"reverse_audit": {"model_provider": "google"}},
            }
        )
        self.assertIn("REAL REVERSE PROMPT", html)
        self.assertIn('id="bdReversePromptText"', html)
        self.assertEqual(html.count('class="sc-card"'), 1)
        for label in ("主体", "场景", "构图", "动作", "光影", "风格", "参数"):
            self.assertNotIn(f">{label}<", html)

    def test_authoritative_timeline_transitions_and_scores_are_visible(self):
        html = self._render(
            {
                "type": "breakdown_reverse",
                "prompt": "[0.0-4.2秒] 第一段\n[4.2-8.0秒] 第二段",
                "quality_score": {
                    "total": 92,
                    "components": {
                        "source_evidence_coverage": 100,
                        "generation_readiness": 92,
                        "factual_consistency": 100,
                    },
                },
                "reverse_audit": {
                    "segments": [
                        {
                            "segment_id": 1,
                            "start_seconds": 0,
                            "end_seconds": 4.2,
                            "readiness": {"ready": 24, "applicable": 26},
                            "transition_from_previous": {
                                "type": "none",
                                "description": "首段",
                            },
                        },
                        {
                            "segment_id": 2,
                            "start_seconds": 4.2,
                            "end_seconds": 8,
                            "readiness": {"ready": 26, "applicable": 26},
                            "transition_from_previous": {
                                "type": "hard_cut",
                                "description": "背景瞬时切换",
                            },
                        },
                    ]
                },
            }
        )
        self.assertIn("综合 92", html)
        self.assertIn("证据覆盖 100", html)
        self.assertIn("生成就绪 92", html)
        self.assertIn("事实一致 100", html)
        self.assertIn("0.0–4.2 秒", html)
        self.assertIn("4.2–8.0 秒", html)
        self.assertIn("直接硬切（背景瞬时切换）", html)
        self.assertIn("生成槽位 24/26", html)

    def test_sections_only_legacy_history_falls_back_to_one_text_card(self):
        html = self._render(
            {
                "prompt": "",
                "sections": {
                    "subject": "legacy subject",
                    "scene": "legacy scene",
                    "parameters": "legacy parameters",
                },
            }
        )
        self.assertIn("主体：legacy subject", html)
        self.assertIn("场景：legacy scene", html)
        self.assertIn("参数：legacy parameters", html)
        self.assertEqual(html.count('class="sc-card"'), 1)

    def test_failure_message_is_not_rendered_as_a_reverse_prompt(self):
        html = self._render(
            {
                "type": "breakdown_reverse",
                "prompt": "反推失败：模型返回异常 · 已退点",
                "source_url": "https://example.invalid/video",
            }
        )
        self.assertNotIn('id="bdReversePromptText"', html)
        self.assertIn("反推结果为空，请重试", html)


if __name__ == "__main__":
    unittest.main()
