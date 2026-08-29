import copy
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERMES = Path(__file__).parents[1] / "server" / "hermes_ip12"
sys.path.insert(0, str(HERMES))

import coaching_skills
import ip12_harness as harness
import project_memory


def checkpoint_pending(state, module, step, content):
    state = harness.normalize_state(state)
    state.pop("choice_generation", None)
    state["pending"] = {
        "id": f"m{module}-s{step}-r{state['revision']}",
        "kind": "checkpoint",
        "status": "awaiting_confirmation",
        "module": module,
        "step": step,
        "draft": content,
        "self_review": "仅使用已确认资料。",
        "profile_updates": [],
        "confidence": 1.0,
    }
    return state


def choice_pending(state, module):
    state = harness.normalize_state(state)
    state.pop("choice_generation", None)
    choices = [
        {
            "choice_id": f"m{module}-choice-{index}", "display_index": index,
            "title": f"方向{index}", "summary": f"模块{module}摘要{index}",
            "reason": f"适配{index}", "caution": f"边界{index}",
            "recommended": index == 2,
        }
        for index in (1, 2, 3)
    ]
    state["pending"] = {
        "id": f"m{module}-choice-r{state['revision']}",
        "kind": "checkpoint", "status": "awaiting_confirmation",
        "module": module, "step": 2, "draft": "",
        "self_review": "三个候选均有差异。", "profile_updates": [],
        "confidence": 1.0, "choices": choices,
    }
    return state


def complete_foundation_state(name="人物"):
    state = harness.initial_state(coaching_skills.SKILL_PIPELINE_V1)
    state["intake"] = {
        "status": "complete", "round": 3, "answers": {},
        "asked_follow_ups": [], "declined_fields": [],
    }
    state["ip_profile"]["facts"]["preferred_name"] = {
        "value": name, "evidence_quote": name,
    }
    for field in harness.INTAKE_COVERAGE_FIELDS:
        if field == "preferred_name":
            continue
        value = "真诚、克制、行动" if field == "personality_traits" else f"{name}-{field}"
        state["ip_profile"]["facts"][field] = {
            "value": value, "evidence_quote": value,
        }
    state = harness.normalize_state(state)
    assert harness.intake_coverage_gaps(state) == []
    for module in (1, 2, 3):
        state = checkpoint_pending(state, module, 1, f"关键词：{name}、真实、行动")
        state, _ = harness.apply_action(
            state, {"type": "confirm_checkpoint", "target_id": state["pending"]["id"]},
            state["revision"], pipeline_version=coaching_skills.SKILL_PIPELINE_V1,
        )
        state = choice_pending(state, module)
        state, _ = harness.apply_action(
            state, {
                "type": "select_checkpoint_choice", "target_id": state["pending"]["id"],
                "choice_id": "choice-2",
            }, state["revision"], request_id=f"select-{name}-{module}",
            pipeline_version=coaching_skills.SKILL_PIPELINE_V1,
        )
    for step in (1, 2, 3, 4):
        content = (
            f"### {name}故事断点{step}\n事实原话：{name}确认的真实经历{step}\n"
            "传播建议：只作为表达建议，不增加结果。"
        )
        state = checkpoint_pending(state, 4, step, content)
        state, _ = harness.apply_action(
            state, {"type": "confirm_checkpoint", "target_id": state["pending"]["id"]},
            state["revision"], pipeline_version=coaching_skills.SKILL_PIPELINE_V1,
        )
    return state


class IP12SixSkillPipelineTests(unittest.TestCase):
    def test_registry_has_the_six_required_skills(self):
        self.assertEqual(set(coaching_skills.SKILL_REGISTRY), {
            "intake", "module_1_positioning", "module_2_persona",
            "module_3_value", "module_4_story", "foundation_pdf",
        })
        self.assertFalse(coaching_skills.SKILL_REGISTRY["foundation_pdf"].model_required)
        for spec in coaching_skills.SKILL_REGISTRY.values():
            self.assertTrue(spec.input_projection)
            self.assertTrue(spec.output_schema)
            self.assertTrue(spec.semantic_validator)
            self.assertEqual(spec.trace_id, spec.skill_id)
        self.assertEqual(harness.SCHEMA_VERSION, 2)

    def test_both_frontends_expose_the_double_confirmation_gate(self):
        for filename in ("index.html", "index_clean.html"):
            page = (HERMES / "templates" / filename).read_text(encoding="utf-8")
            self.assertIn("awaiting_snapshot_confirmation", page, filename)
            self.assertIn("confirm_foundation_snapshot", page, filename)
            self.assertIn("reopen_foundation_module", page, filename)
            self.assertIn("snapshot_sha256", page, filename)
        main_page = (HERMES / "templates/index.html").read_text(encoding="utf-8")
        self.assertIn("confirm_foundation_snapshot'){closePanel()", main_page)

    def test_legacy_state_never_enters_the_new_snapshot_gate(self):
        state = harness.initial_state()
        self.assertEqual(state["pipeline_version"], "legacy")
        self.assertEqual(coaching_skills.project_pipeline_version({"coach_state": state}), "legacy")
        self.assertEqual(coaching_skills.normalize_pipeline_version("v1"), "ip12-skills-v1")

    def test_module_four_builds_and_confirms_a_frozen_snapshot(self):
        state = complete_foundation_state("林舟")
        report = state["foundation_report"]
        self.assertEqual(report["status"], "awaiting_snapshot_confirmation")
        memory = project_memory.build({"pipeline_version": coaching_skills.SKILL_PIPELINE_V1}, state)
        self.assertEqual(memory["workflow"]["active_skill_id"], "foundation_pdf")
        self.assertEqual(len(report["snapshot"]["modules"]), 4)
        self.assertTrue(report["snapshot"]["sha256"].startswith("sha256:"))
        self.assertEqual(state["completed_modules"], [1, 2, 3, 4])
        action = harness.available_actions(state)[0]
        self.assertEqual(action["type"], "confirm_foundation_snapshot")
        next_state, event = harness.apply_action(
            state, action, state["revision"], selected_at="2026-08-29T00:00:00Z",
            pipeline_version=coaching_skills.SKILL_PIPELINE_V1,
        )
        self.assertEqual(next_state["foundation_report"]["status"], "generating")
        self.assertTrue(event["generate_foundation_report"])
        coaching_skills.validate_foundation_snapshot(
            next_state["foundation_report"]["snapshot"], next_state
        )

    def test_pdf_confirmation_cannot_bypass_server_artifact_validation(self):
        state = complete_foundation_state("确认门")
        snapshot_action = harness.available_actions(state)[0]
        state, _ = harness.apply_action(
            state, snapshot_action, state["revision"],
            pipeline_version=coaching_skills.SKILL_PIPELINE_V1,
        )
        state["foundation_report"].update(status="awaiting_confirmation", report_id="report-v1")
        action = {"type": "confirm_foundation_report", "target_id": "report-v1"}
        with self.assertRaises(harness.HarnessConflict):
            harness.apply_action(
                state, action, state["revision"],
                pipeline_version=coaching_skills.SKILL_PIPELINE_V1,
            )
        confirmed, _ = harness.apply_action(
            state, action, state["revision"],
            pipeline_version=coaching_skills.SKILL_PIPELINE_V1,
            foundation_artifact_validated=True,
        )
        self.assertEqual(confirmed["foundation_report"]["status"], "confirmed")
        self.assertEqual(confirmed["current_module"], 5)

    def test_next_skill_input_excludes_unselected_candidates(self):
        state = complete_foundation_state("候选隔离")
        projection = coaching_skills.confirmed_input_projection(state)
        serialized = __import__("json").dumps(projection, ensure_ascii=False)
        self.assertNotIn("模块1摘要1", serialized)
        self.assertNotIn("choice_snapshot", serialized)
        self.assertNotIn("report_payload", serialized)
        self.assertIn("模块1摘要2", serialized)

    def test_reopening_module_cascades_through_module_four(self):
        state = complete_foundation_state("苏禾")
        report = state["foundation_report"]
        action = {
            "type": "reopen_foundation_module",
            "target_id": report["snapshot"]["snapshot_id"],
            "module": 2,
        }
        next_state, _ = harness.apply_action(
            state, action, state["revision"],
            pipeline_version=coaching_skills.SKILL_PIPELINE_V1,
        )
        self.assertEqual(next_state["current_module"], 2)
        self.assertEqual(next_state["completed_modules"], [1])
        self.assertTrue(all(not key.startswith(("2-", "3-", "4-"))
                            for key in next_state["ip_profile"]["confirmed_outputs"]))
        self.assertEqual(next_state["foundation_report"]["status"], "reopening")
        self.assertEqual(next_state["foundation_report"]["superseded_snapshot"]["snapshot_id"],
                         report["snapshot"]["snapshot_id"])
        self.assertEqual(len(next_state["foundation_report"]["snapshot_history"]), 1)

    def test_deterministic_compiler_is_stable_and_person_isolated(self):
        hashes = set()
        for index in range(10):
            name = f"虚构人物{index}"
            state = complete_foundation_state(name)
            snapshot = state["foundation_report"]["snapshot"]
            first = coaching_skills.compile_foundation_markdown(snapshot)
            second = coaching_skills.compile_foundation_markdown(copy.deepcopy(snapshot))
            self.assertEqual(first, second)
            self.assertIn(name, first)
            self.assertNotIn(f"虚构人物{(index + 1) % 10}", first)
            self.assertNotIn(f"{name}-mobile", first)
            self.assertNotIn(f"{name}-income_range", first)
            hashes.add(snapshot["sha256"])
        self.assertEqual(len(hashes), 10)

    @unittest.skipUnless(
        importlib.util.find_spec("flask") and importlib.util.find_spec("requests"),
        "Flask runtime dependencies are required",
    )
    def test_new_projects_use_v1_and_legacy_projects_do_not_migrate(self):
        script = r'''
import json
import security
import server

security._validate_token = lambda _token: {"account_id":"pipeline-test","username":"pipeline","role":"member"}
client = server.app.test_client()
headers = {"Authorization":"Bearer test"}
created = client.post("/api/conversations", json={"title":"Skill Project"}, headers=headers)
assert created.status_code == 200, created.get_data(as_text=True)
cid = created.get_json()["id"]
project = server.load_conversation(cid)
assert project["pipeline_version"] == "ip12-skills-v1", project
assert project["coach_state"]["pipeline_version"] == "ip12-skills-v1", project["coach_state"]
skill_state = project["coach_state"]
skill_state["intake"]["status"] = "complete"
skill_state["ip_profile"]["facts"]["preferred_name"] = {"value":"Skill用户","evidence_quote":"Skill用户"}
skill_state["ip_profile"]["ai_selections"]["stale_candidate"] = {"value":"不能进入新Skill","evidence_quote":""}
captured = []
class Response:
    def json(self):
        return {"choices":[{"message":{"content":json.dumps({
            "decision":"answer_only", "checkpoint":0, "reply":"当前仍在模块一。",
            "draft":"", "self_review":"", "choices":[], "profile_updates":[], "confidence":1,
        }, ensure_ascii=False)}}]}
def capture(messages, **_kwargs):
    captured.extend(messages)
    return Response()
server.call_ai = capture
decision, _ = server._coach_model_decision({
    "coach_state":skill_state, "pipeline_version":"ip12-skills-v1", "messages":[], "deliverables":{},
}, "现在到哪")
assert decision["_trace_skill"] == "module_1_positioning", decision
assert "不能进入新Skill" not in json.dumps(captured, ensure_ascii=False), captured

legacy_id = "legacy-project"
legacy = {
    "id":legacy_id, "title":"Legacy", "owner_account_id":"pipeline-test",
    "messages":[], "coach_state":server.coach_harness.initial_state(),
    "reports":{}, "deliverables":{},
}
server.save_conversation(legacy_id, legacy)
public = client.get(f"/api/conversations/{legacy_id}", headers=headers).get_json()
assert public["pipeline_version"] == "legacy", public
assert public["coach_state"]["pipeline_version"] == "legacy", public["coach_state"]

backup = server._project_backup_payload(cid, project)
assert backup["project"]["pipeline_version"] == "ip12-skills-v1", backup
restored = server._parse_project_backup(json.dumps(backup).encode())
assert restored["pipeline_version"] == "ip12-skills-v1", restored
print("PIPELINE_PROJECT_COMPAT_OK")
'''
        with tempfile.TemporaryDirectory() as data_dir:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy", HERMES_HOME=data_dir, HERMES_DATA_DIR=data_dir,
                HERMES_IP12_SKILL_PIPELINE_DEFAULT=coaching_skills.SKILL_PIPELINE_V1,
            )
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=HERMES, env=env,
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PIPELINE_PROJECT_COMPAT_OK", result.stdout)

    @unittest.skipUnless(
        importlib.util.find_spec("flask") and importlib.util.find_spec("requests"),
        "Flask runtime dependencies are required",
    )
    def test_intake_clarification_is_answered_without_model_or_state_advance(self):
        script = r'''
import security
import server

security._validate_token = lambda _token: {"account_id":"clarify-test","username":"clarify","role":"member"}
cid = "clarification-project"
state = server.coach_harness.initial_state(server.coaching_skills.SKILL_PIPELINE_V1)
state["intake"]["asked_follow_ups"] = ["target_audience"]
state["intake"]["current_question_field"] = "target_audience"
server.save_conversation(cid, {
    "id":cid, "title":"澄清测试", "owner_account_id":"clarify-test",
    "pipeline_version":server.coaching_skills.SKILL_PIPELINE_V1,
    "messages":[], "coach_state":state, "reports":{}, "deliverables":{},
})
server.call_ai = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("clarification must not call model"))
client = server.app.test_client()
response = client.post("/api/chat-complete", headers={"Authorization":"Bearer test"}, json={
    "conversation_id":cid, "expected_revision":state["revision"],
    "request_id":"clarify-request-001", "message":"你能多说几个字吗？我不太理解",
})
assert response.status_code == 200, response.get_data(as_text=True)
payload = response.get_json()
assert "最想长期帮助哪一类人" in payload["assistant"], payload
assert "想靠 AI 接单的人" in payload["assistant"], payload
assert "我已记下" not in payload["assistant"], payload
next_state = payload["state"]
assert next_state["intake"]["asked_follow_ups"] == ["target_audience"], next_state["intake"]
assert next_state["intake"].get("profile_updates", []) == [], next_state["intake"]
print("INTAKE_CLARIFICATION_OK")
'''
        with tempfile.TemporaryDirectory() as data_dir:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy", HERMES_HOME=data_dir, HERMES_DATA_DIR=data_dir,
                HERMES_IP12_SKILL_PIPELINE_DEFAULT="v1",
            )
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=HERMES, env=env,
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("INTAKE_CLARIFICATION_OK", result.stdout)

    @unittest.skipUnless(
        importlib.util.find_spec("reportlab") and importlib.util.find_spec("pypdf"),
        "reportlab and pypdf are required",
    )
    def test_foundation_pdf_generation_never_calls_model(self):
        script = r'''
import copy
import coaching_skills
import ip12_harness
import server

def choice_payload(module):
    choices = [{
        "choice_id": f"choice-{index}", "display_index": index,
        "title": f"方向{index}", "summary": f"模块{module}摘要{index}",
        "reason": f"适配{index}", "caution": f"边界{index}", "recommended": index == 2,
    } for index in (1, 2, 3)]
    return {
        "skill_id": coaching_skills.MODULE_SKILL_IDS[module], "skill_version": "1.0.0",
        "module": module, "module_name": coaching_skills.MODULE_NAMES[module],
        "keywords": ["真实", "行动", "长期"], "final_conclusion": f"方向2：模块{module}摘要2",
        "candidates": choices, "selected_choice_id": "choice-2", "selected_basis": "适配2",
        "communication_card": {"core_expression": f"模块{module}摘要2", "usage": "账号简介", "boundary": "边界2"},
    }

state = ip12_harness.initial_state(coaching_skills.SKILL_PIPELINE_V1)
state.update(current_module=4, module_step=4, completed_modules=[1,2,3,4], revision=11)
state["intake"].update(status="complete", round=3)
outputs = state["ip_profile"]["confirmed_outputs"]
for module in (1,2,3):
    outputs[f"{module}-2"] = {"report_payload": choice_payload(module)}
story = {
    "skill_id": "module_4_story", "skill_version": "1.0.0", "module": 4,
    "module_name": "故事资产挖掘", "final_conclusion": "事实原话：虚构测试经历",
    "sections": [{"checkpoint": i, "title": f"故事断点{i}", "content": f"事实原话：虚构测试经历{i}"} for i in range(1,5)],
    "evidence_quotes": ["虚构测试经历1"],
}
outputs["4-4"] = {"report_payload": story}
snapshot = coaching_skills.build_foundation_snapshot(state)
state["foundation_report"] = {"status":"generating", "snapshot":snapshot, "snapshot_sha256":snapshot["sha256"], "snapshot_confirmed_at":"2026-08-29T00:00:00Z"}
cid = "deterministic-pdf"
server.save_conversation(cid, {
    "id":cid, "title":"确定性PDF", "pipeline_version":coaching_skills.SKILL_PIPELINE_V1,
    "owner_account_id":"test", "messages":[], "coach_state":state, "reports":{}, "deliverables":{},
})
server.call_ai = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("model must not run"))
report = server.generate_foundation_report(cid)
assert report["status"] == "awaiting_confirmation", report
assert report["template_version"] == server.FOUNDATION_SKILL_TEMPLATE_VERSION, report
assert report["snapshot_sha256"] == snapshot["sha256"], report
assert report["artifact"]["input_snapshot_sha256"] == snapshot["sha256"], report
assert report["artifact"]["content_sha256"] == report["content_sha256"], report
assert report["agent_trace"]["skills"][0]["id"] == "foundation_pdf", report
assert report["snapshot_confirmed_at"] == "2026-08-29T00:00:00Z", report
assert 6 <= report["artifact"]["page_count"] <= 8, report
assert "首页｜IP结论总览" in report["content"]
broken = dict(report)
broken["artifact"] = dict(report["artifact"])
broken["artifact"].pop("input_snapshot_sha256")
try:
    server._validate_foundation_artifact(broken, server.FOUNDATION_REPORTS_DIR / f"{cid}.pdf", strict=True)
except RuntimeError:
    pass
else:
    raise AssertionError("strict artifact validation must fail closed")
server.current_account_id = lambda: "test"
payload = {"conversation_id":cid, "expected_revision":server.load_conversation(cid)["coach_state"]["revision"], "report_id":report["report_id"], "request_id":"confirm-pdf-0001"}
with server.app.test_request_context("/api/foundation-report/confirm", method="POST", json=payload):
    first = server.api_confirm_foundation_report().get_json()
assert first["ok"] and first["state"]["current_module"] == 5, first
with server.app.test_request_context("/api/foundation-report/confirm", method="POST", json=payload):
    second = server.api_confirm_foundation_report().get_json()
assert second["ok"] and second["replayed"] is True, second
print("DETERMINISTIC_FOUNDATION_PDF_OK")
'''
        with tempfile.TemporaryDirectory() as data_dir:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy", HERMES_HOME=data_dir, HERMES_DATA_DIR=data_dir,
                HERMES_IP12_SKILL_PIPELINE_DEFAULT=coaching_skills.SKILL_PIPELINE_V1,
            )
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=HERMES, env=env,
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DETERMINISTIC_FOUNDATION_PDF_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
