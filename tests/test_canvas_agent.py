import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import canvas_agent, core, feature_flags, registry


def payload(**updates):
    data = {
        "prompt": "把卖点整理成图片生成草稿",
        "project_id": "local:board_1",
        "snapshot_digest": "1234abcd",
        "scope": "local",
        "nodes": [
            {"id": "n1", "type": "text", "title": "卖点", "content": "轻薄，适合通勤", "selected": True},
            {"id": "n2", "type": "gen", "title": "作图", "content": "", "selected": False},
        ],
        "edges": [],
        "selected_node_ids": ["n1"],
        "history": [],
        "quoted_cost": 3,
    }
    data.update(updates)
    return data


class CanvasAgentTests(unittest.TestCase):
    def test_registered_with_safe_price_and_disabled_default(self):
        self.assertIs(registry.HANDLERS["canvas_agent"], canvas_agent.gen_canvas_agent)
        self.assertEqual(core.COST["canvas_agent"], 3)
        self.assertFalse(feature_flags.CATALOG_MAP["canvas_agent"]["default_enabled"])

    def test_quote_is_flagged_and_uses_current_balance(self):
        class Handler:
            def _json_body_strict(self): return {}
            def _send(self, status, body): return status, body

        with mock.patch.object(feature_flags, "require_enabled") as enabled, \
             mock.patch("content_domains.points.get_points", return_value=19):
            status, body = canvas_agent.handle_quote(Handler(), {"username": "tester"})
        enabled.assert_called_once_with("canvas_agent")
        self.assertEqual((status, body["cost"], body["points"]), (200, 3, 19))

    def test_snapshot_rejects_media_and_unverified_collaboration(self):
        with self.assertRaisesRegex(ValueError, "媒体数据"):
            canvas_agent.validate_payload(payload(nodes=[{
                "id": "n1", "type": "image", "title": "图", "content": "data:image/png;base64,AAAA", "selected": True,
            }]))
        collab = payload(project_id="collab:board_8", scope="collab")
        with self.assertRaisesRegex(PermissionError, "无访问权限"):
            canvas_agent.validate_payload(collab)
        clean = canvas_agent.validate_payload(collab, {"board_id": "board_8", "role": "viewer"})
        self.assertEqual(clean["project_id"], "collab:board_8")

    def test_page_and_ip12_context_are_bounded(self):
        clean = canvas_agent.validate_payload(payload(
            page_context={
                "page": "canvas", "path": "/workbench/canvas", "title": "黄雀画布",
                "can_edit": True, "selected_count": 1,
            },
            ip12_context={
                "project_id": "ip12_project_1", "title": "美业 IP", "status": "confirmed",
                "foundation_status": "confirmed",
                "facts": [{"label": "定位", "value": "问题肌管理主理人"}],
            },
        ))
        self.assertEqual(clean["page_context"]["page"], "canvas")
        self.assertEqual(clean["ip12_context"]["facts"][0]["label"], "定位")
        with self.assertRaisesRegex(ValueError, "页面上下文"):
            canvas_agent.validate_payload(payload(page_context={
                "page": "other", "path": "/admin", "title": "后台", "can_edit": True, "selected_count": 0,
            }))

    def test_plan_copies_snapshot_and_only_updates_selected_text(self):
        request = canvas_agent.validate_payload(payload())
        raw = json.dumps({
            "content": "已整理，可确认应用。",
            "actions": [{"type": "update_text_node", "node_id": "n1", "title": "通勤卖点", "content": "轻薄，便于补涂"}],
            "warnings": [],
        }, ensure_ascii=False)
        result = canvas_agent.normalize_model_result(raw, request)
        self.assertEqual(result["plan"]["project_id"], request["project_id"])
        self.assertEqual(result["plan"]["snapshot_digest"], request["snapshot_digest"])
        self.assertTrue(result["plan"]["requires_confirmation"])
        self.assertEqual(result["plan"]["actions"][0]["id"], "action_1")

        bad = json.dumps({"content": "", "actions": [
            {"type": "update_text_node", "node_id": "n2", "content": "越权修改"}
        ], "warnings": []}, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "只能修改"):
            canvas_agent.normalize_model_result(bad, request)

    def test_generation_action_creates_draft_but_never_executes_generation(self):
        request = canvas_agent.validate_payload(payload())
        raw = json.dumps({
            "content": "已准备图片草稿。",
            "actions": [{
                "type": "create_generation_draft", "mode": "image", "title": "通勤主视觉",
                "prompt": "清透通勤妆感，9:16", "connect_from": ["n1"],
            }],
            "warnings": ["草稿不会自动生成图片"],
        }, ensure_ascii=False)
        result = canvas_agent.normalize_model_result(raw, request)
        action = result["plan"]["actions"][0]
        self.assertEqual(action["type"], "create_generation_draft")
        self.assertNotIn("execute", action)
        self.assertNotIn("url", json.dumps(result, ensure_ascii=False).lower())

    def test_guides_only_target_known_huangque_pages(self):
        request = canvas_agent.validate_payload(payload())
        raw = json.dumps({
            "content": "建议先完善视觉提示词。", "actions": [], "warnings": [],
            "guides": [{
                "target": "image", "label": "去图片工作台", "reason": "继续制作主视觉",
                "prompt": "真实门店主理人半身像",
            }],
        }, ensure_ascii=False)
        result = canvas_agent.normalize_model_result(raw, request)
        self.assertEqual(result["plan"]["guides"][0]["target"], "image")
        self.assertFalse(result["plan"]["requires_confirmation"])
        bad = raw.replace('"image"', '"https://evil.example"', 1)
        with self.assertRaisesRegex(ValueError, "引导目标"):
            canvas_agent.normalize_model_result(bad, request)

    def test_model_call_uses_terra_responses_with_bounded_structured_context(self):
        reply = json.dumps({"content": "可以。", "actions": [], "guides": [], "warnings": []}, ensure_ascii=False)
        captured = {}

        def fake_post(path, body, content_type, timeout):
            captured.update(path=path, body=json.loads(body), content_type=content_type, timeout=timeout)
            return {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": reply},
            ]}]}

        with mock.patch.object(canvas_agent, "_post", side_effect=fake_post):
            result = canvas_agent.gen_canvas_agent(payload())
        self.assertEqual(result["content"], "可以。")
        self.assertEqual(captured["path"], "/v1/responses")
        self.assertEqual(captured["body"]["model"], "gpt-5.6-terra")
        self.assertEqual(captured["body"]["reasoning"], {"effort": "low"})
        self.assertTrue(captured["body"]["text"]["format"]["strict"])
        self.assertEqual(captured["body"]["text"]["format"]["schema"], canvas_agent.CANVAS_AGENT_SCHEMA)
        self.assertFalse(captured["body"]["store"])
        self.assertEqual(json.loads(captured["body"]["input"])["task"], payload()["prompt"])
        self.assertEqual(captured["timeout"], 120)


if __name__ == "__main__":
    unittest.main()
