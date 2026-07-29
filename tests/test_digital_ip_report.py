import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import digital_ip


def _complete_state():
    answers = {}
    for module_index, step_count in enumerate(
            digital_ip.PROJECT_MODULE_STEPS[:digital_ip.FOUNDATION_MODULES]):
        for step_index in range(step_count):
            answers["%d-%d" % (module_index, step_index)] = {
                "text": "获客成本高，老客复购下降" if (module_index, step_index) == (0, 0) else "",
                "confirmed": (module_index, step_index) == (0, 0),
                "skipped": (module_index, step_index) != (0, 0),
            }
    return {"questionnaire_state": {"answers": answers, "profile": {"定位": "真实门店经营者"}}}


def _report():
    return {
        "title": "美业 IP 人设定位阶段报告",
        "executive_summary": "先以真实经营问题建立可信定位，再补齐人设与故事事实。",
        "evidence": [{
            "evidence_id": "E1",
            "claim": "门店面临获客与复购压力",
            "source_ref": "answer:0-0",
            "source_excerpt": "获客成本高，老客复购下降",
        }],
        "modules": [
            {
                "module_id": module_id, "title": title, "summary": "只写已确认事实与待验证建议。",
                "findings": ([{
                    "kind": "fact", "title": "真实经营问题",
                    "detail": "获客成本高，老客复购下降。", "evidence_ids": ["E1"], "risks": [],
                }] if module_id == 1 else []),
            }
            for module_id, title in enumerate(("定位诊断", "人设塑造", "价值主张", "故事资产"), 1)
        ],
        "execution_priorities": [{
            "priority": "P0", "module_id": 1, "task": "核对定位事实",
            "output": "一版可确认定位", "evidence_ids": ["E1"],
        }],
        "confirmation_items": [],
        "material_gaps": [{
            "gap": "其余采访问题未提供资料", "why_needed": "限制完整人设定位",
            "how_to_collect": "回到项目补充被跳过步骤", "blocking": False,
            "source_refs": [
                "answer:%d-%d" % (module_index, step_index)
                for module_index, step_count in enumerate(
                    digital_ip.PROJECT_MODULE_STEPS[:digital_ip.FOUNDATION_MODULES])
                for step_index in range(step_count)
                if (module_index, step_index) != (0, 0)
            ],
        }],
        "disclaimer": "仅基于用户确认资料；AI 推断需本人复核，不保证经营结果。",
    }


def _response(report=None):
    return {
        "status": "completed",
        "model": "gpt-5.6-sol-test",
        "output": [{"type": "message", "content": [{
            "type": "output_text", "text": json.dumps(report or _report(), ensure_ascii=False),
        }]}],
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
    }


class DigitalIPReportTests(unittest.TestCase):
    def setUp(self):
        digital_ip._report_recent_requests.clear()
        digital_ip._report_daily_requests.clear()
        digital_ip._project_inflight.clear()
        digital_ip._project_actions.clear()
        digital_ip._project_mutations.clear()

    def _project(self):
        project = digital_ip.create_project("owner", {"title": "门店 IP"})
        return digital_ip.patch_project("owner", project["id"], {
            "revision": project["revision"], "state": _complete_state(),
        })

    def test_requires_all_foundation_questions_before_paid_model_call(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = digital_ip.create_project("owner", {})
            with mock.patch.object(digital_ip, "_post") as post, \
                    self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "模块 1–4 的采访问题"):
                digital_ip.generate_report("owner", project["id"], {"revision": project["revision"], "consent": True})
            post.assert_not_called()
            self.assertEqual(digital_ip.get_project("owner", project["id"])["revision"], project["revision"])

    def test_explicit_consent_is_required_before_model_call(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = self._project()
            with mock.patch.object(digital_ip, "_post") as post, \
                    self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "明确同意"):
                digital_ip.generate_report("owner", project["id"], {"revision": project["revision"]})
            post.assert_not_called()
            with self.assertRaises(digital_ip.DigitalIPNotFound):
                digital_ip.get_report("owner", project["id"])

    def test_structured_report_is_owned_persisted_and_preserved_by_later_patch(self):
        captured = {}

        def fake_post(path, body, content_type, timeout):
            captured.update(path=path, body=json.loads(body), timeout=timeout)
            return _response()

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = self._project()
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                    mock.patch.object(digital_ip, "_post", side_effect=fake_post):
                result = digital_ip.generate_report("owner", project["id"], {"revision": project["revision"], "consent": True})

            self.assertEqual(captured["path"], "/v1/responses")
            self.assertFalse(captured["body"]["store"])
            self.assertTrue(captured["body"]["text"]["format"]["strict"])
            self.assertEqual(captured["body"]["text"]["format"]["type"], "json_schema")
            prompt = json.loads(captured["body"]["input"][0]["content"][0]["text"])
            self.assertEqual(prompt["confirmed_answers"], [{
                "source_ref": "answer:0-0", "module_id": 1, "module_name": "定位诊断",
                "step_index": 1, "step_title": "姓名或昵称", "source_kind": "fact",
                "answer": "获客成本高，老客复购下降",
            }])
            self.assertEqual(len(prompt["skipped_steps"]), 29)
            self.assertNotIn("product_catalog", prompt)
            self.assertFalse(result["stale"])
            self.assertNotIn("model", result["report"])
            self.assertEqual(result["report"]["stage"], digital_ip.FOUNDATION_STAGE)
            self.assertEqual(result["report"]["status"], "pending_confirmation")
            self.assertEqual(result["report"]["progress"], {"total": 30, "confirmed": 1, "skipped": 29, "unresolved": 0})
            loaded_report = digital_ip.get_report("owner", project["id"])["report"]
            self.assertNotIn("model", loaded_report)
            self.assertEqual(loaded_report["report_id"], result["report"]["report_id"])
            with self.assertRaises(digital_ip.DigitalIPNotFound):
                digital_ip.get_report("other", project["id"])

            changed = digital_ip.patch_project("owner", project["id"], {
                "revision": result["project"]["revision"],
                "state": {"questionnaire_state": {"answers": {}}},
            })
            self.assertNotIn(digital_ip.REPORT_STATE_KEY, changed["state"])
            self.assertTrue(digital_ip.get_report("owner", project["id"])["stale"])
            with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "只允许问卷草稿字段"):
                digital_ip._clean_state({digital_ip.REPORT_STATE_KEY: {"forged": True}})

    def test_provider_failure_and_untraceable_finding_do_not_persist(self):
        invalid = _report()
        invalid["modules"][0]["findings"][0]["evidence_ids"] = ["E404"]
        for response in ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, _response(invalid)):
            with self.subTest(response=response.get("status", "completed")), tempfile.TemporaryDirectory() as directory, \
                    mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
                project = self._project()
                with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                        mock.patch.object(digital_ip, "_post", return_value=response), \
                        self.assertRaises(digital_ip.DigitalIPError):
                    digital_ip.generate_report("owner", project["id"], {"revision": project["revision"], "consent": True})
                current = digital_ip.get_project("owner", project["id"])
                self.assertEqual(current["revision"], project["revision"])
                with self.assertRaises(digital_ip.DigitalIPNotFound):
                    digital_ip.get_report("owner", project["id"])
                self.assertNotIn("owner", digital_ip._report_recent_requests)
                self.assertFalse(digital_ip._report_daily_requests)

    def test_provider_failure_releases_report_quota(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = self._project()
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                    mock.patch.object(digital_ip, "_post", side_effect=OSError("offline")), \
                    self.assertRaises(digital_ip.DigitalIPError):
                digital_ip.generate_report("owner", project["id"], {"revision": project["revision"], "consent": True})
        self.assertNotIn("owner", digital_ip._report_recent_requests)
        self.assertFalse(digital_ip._report_daily_requests)

    def test_same_project_revision_uses_one_inflight_report_call(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def fake_post(*_args, **_kwargs):
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            return _response()

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = self._project()
            payload = {"revision": project["revision"], "consent": True}
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                    mock.patch.object(digital_ip, "_post", side_effect=fake_post):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(digital_ip.generate_report, "owner", project["id"], payload)
                    self.assertTrue(entered.wait(2))
                    second = pool.submit(digital_ip.generate_report, "owner", project["id"], payload)
                    time.sleep(0.05)
                    self.assertEqual(calls, [1])
                    release.set()
                    self.assertEqual(first.result()["report"]["report_id"], second.result()["report"]["report_id"])

    def test_confirmed_attachment_evidence_enters_report_without_raw_file(self):
        captured = []
        attachment = "经营资料.pdf"
        encoded = "cHJvb2Y="
        analysis = {
            "positioning_candidates": [{"title": "候选一"}, {"title": "候选二"}, {"title": "候选三"}],
            "source_evidence": [
                {"claim": "附件中有复购数据", "evidence": "复购率 35%", "file_name": attachment, "location": "第 2 页"},
                {"claim": "当前回答", "evidence": "获客成本高", "file_name": "用户当前回答", "location": "未定位"},
            ],
        }
        analysis_response = {"model": "test", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(analysis, ensure_ascii=False)},
        ]}]}

        attachment_report = _report()
        attachment_report["evidence"].append({
            "evidence_id": "E2", "claim": "附件复购率可核验",
            "source_ref": "answer:0-0:attachment:1", "source_excerpt": "复购率 35%",
        })
        changed_report = _report()
        changed_report["evidence"][0]["source_excerpt"] = "已变更的原始回答"
        for report in (attachment_report, changed_report):
            report["material_gaps"][0]["source_refs"] = [
                ref for ref in report["material_gaps"][0]["source_refs"] if ref != "answer:0-1"
            ]
        report_responses = [_response(attachment_report), _response(changed_report)]

        def report_post(path, body, content_type, timeout):
            captured.append({"path": path, "body": json.loads(body), "content_type": content_type, "timeout": timeout})
            return report_responses.pop(0)

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = self._project()
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                    mock.patch.object(digital_ip, "_post", return_value=analysis_response):
                analyzed = digital_ip.analyze_project("owner", project["id"], {
                    "revision": project["revision"], "module_index": 0, "step_index": 0,
                    "answer": "获客成本高，老客复购下降", "consent": True,
                    "files": [{"name": attachment, "type": "application/pdf", "data_url": "data:application/pdf;base64," + encoded}],
                })
            self.assertEqual(
                digital_ip._report_source(digital_ip._owned_project("owner", project["id"]))["confirmed_attachment_evidence"],
                [],
            )
            confirmed = digital_ip.confirm_project("owner", project["id"], {
                "revision": analyzed["project"]["revision"], "candidate_index": 0,
            })
            with closing(digital_ip._project_db()) as conn:
                persisted = conn.execute("SELECT last_analysis_json,confirmed_json FROM digital_ip_projects WHERE id=?", (project["id"],)).fetchone()
            self.assertNotIn(encoded, persisted["last_analysis_json"])
            self.assertNotIn(encoded, persisted["confirmed_json"])
            self.assertIn(attachment, persisted["confirmed_json"])
            second_state = _complete_state()
            second_state["questionnaire_state"]["answers"]["0-1"] = {
                "text": "第二步已经确认", "confirmed": True, "skipped": False,
            }
            second_draft = digital_ip.patch_project("owner", project["id"], {
                "revision": confirmed["project"]["revision"], "state": second_state,
            })
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                    mock.patch.object(digital_ip, "_post", return_value=analysis_response):
                second_analysis = digital_ip.analyze_project("owner", project["id"], {
                    "revision": second_draft["revision"], "module_index": 0, "step_index": 1,
                    "answer": "第二步已经确认", "consent": True,
                })
            second_confirmed = digital_ip.confirm_project("owner", project["id"], {
                "revision": second_analysis["project"]["revision"], "candidate_index": 0,
            })
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                    mock.patch.object(digital_ip, "_post", side_effect=report_post):
                result = digital_ip.generate_report("owner", project["id"], {"revision": second_confirmed["project"]["revision"], "consent": True})
            changed_state = _complete_state()
            changed_state["questionnaire_state"]["answers"]["0-1"] = {
                "text": "第二步已经确认", "confirmed": True, "skipped": False,
            }
            changed_state["questionnaire_state"]["answers"]["0-0"] = {
                "text": "已变更的原始回答", "confirmed": True, "skipped": False,
            }
            changed = digital_ip.patch_project("owner", project["id"], {
                "revision": result["project"]["revision"], "state": changed_state,
            })
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                    mock.patch.object(digital_ip, "_post", side_effect=report_post):
                digital_ip.generate_report("owner", project["id"], {"revision": changed["revision"], "consent": True})
        prompt = json.loads(captured[0]["body"]["input"][0]["content"][0]["text"])
        self.assertEqual(prompt["confirmed_attachment_evidence"], [{
            "source_ref": "answer:0-0:attachment:1", "file_name": attachment,
            "location": "第 2 页", "claim": "附件中有复购数据", "evidence": "复购率 35%",
        }])
        self.assertEqual(result["report"]["content"]["evidence"][0]["source_name"], "已确认问卷回答")
        self.assertEqual(result["report"]["content"]["evidence"][0]["source_location"], "模块 1 · 姓名或昵称")
        self.assertEqual(result["report"]["content"]["evidence"][1]["source_name"], attachment)
        self.assertEqual(result["report"]["content"]["evidence"][1]["source_location"], "第 2 页")
        changed_prompt = json.loads(captured[1]["body"]["input"][0]["content"][0]["text"])
        self.assertEqual(changed_prompt["confirmed_attachment_evidence"], [])

    def test_report_must_cover_every_skipped_step_as_a_material_gap(self):
        report = _report()
        report["material_gaps"][0]["source_refs"] = []
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = self._project()
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                    mock.patch.object(digital_ip, "_post", return_value=_response(report)), \
                    self.assertRaisesRegex(digital_ip.DigitalIPError, "完整标明"):
                digital_ip.generate_report("owner", project["id"], {"revision": project["revision"], "consent": True})
            with self.assertRaises(digital_ip.DigitalIPNotFound):
                digital_ip.get_report("owner", project["id"])

    def test_cas_conflict_after_model_call_does_not_persist_report(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = self._project()

            def competing_post(*_args, **_kwargs):
                with closing(digital_ip._project_db()) as conn:
                    conn.execute(
                        "UPDATE digital_ip_projects SET title=?, revision=revision+1, updated_at=? WHERE id=?",
                        ("另一端更新", int(time.time()), project["id"]),
                    )
                    conn.commit()
                return _response()

            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                    mock.patch.object(digital_ip, "_post", side_effect=competing_post), \
                    self.assertRaises(digital_ip.DigitalIPRevisionConflict):
                digital_ip.generate_report("owner", project["id"], {"revision": project["revision"], "consent": True})
            current = digital_ip.get_project("owner", project["id"])
            self.assertEqual(current["title"], "另一端更新")
            with self.assertRaises(digital_ip.DigitalIPNotFound):
                digital_ip.get_report("owner", project["id"])

    def test_report_confirmation_is_the_only_gate_for_modules_five_and_six(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"), \
                mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                mock.patch.object(digital_ip, "_post", return_value=_response()):
            project = self._project()
            generated = digital_ip.generate_report(
                "owner", project["id"], {"revision": project["revision"], "consent": True},
            )
            pending_state = json.loads(json.dumps(generated["project"]["state"], ensure_ascii=False))
            pending_state["questionnaire_state"]["answers"]["4-0"] = {"text": "客户最近总问怎么获客"}
            with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "确认未变更"):
                digital_ip.patch_project("owner", project["id"], {
                    "revision": generated["project"]["revision"], "state": pending_state,
                })
            with self.assertRaises(digital_ip.DigitalIPRevisionConflict):
                digital_ip.confirm_report("owner", project["id"], {
                    "revision": generated["project"]["revision"], "report_id": "wrong-report",
                })
            confirmed = digital_ip.confirm_report("owner", project["id"], {
                "revision": generated["project"]["revision"],
                "report_id": generated["report"]["report_id"],
            })["project"]
            self.assertEqual(confirmed["foundation_stage"]["status"], "confirmed")

            content_state = json.loads(json.dumps(confirmed["state"], ensure_ascii=False))
            content_state["questionnaire_state"]["answers"]["4-0"] = {"text": "客户最近总问怎么获客"}
            content_project = digital_ip.patch_project("owner", project["id"], {
                "revision": confirmed["revision"], "state": content_state,
            })
            self.assertFalse(digital_ip.get_report("owner", project["id"])["stale"])
            analysis = {"positioning_candidates": [{"title": "A"}, {"title": "B"}, {"title": "C"}]}
            with mock.patch.object(digital_ip, "_project_analysis", return_value=(analysis, "test", {})):
                analyzed = digital_ip.analyze_project("owner", project["id"], {
                    "revision": content_project["revision"], "module_index": 4, "step_index": 0,
                    "answer": "客户最近总问怎么获客", "consent": True,
                })
            accepted = digital_ip.confirm_project("owner", project["id"], {
                "revision": analyzed["project"]["revision"], "candidate_index": 0,
            })["project"]
            self.assertEqual(accepted["status"], "confirmed")

            changed_foundation = json.loads(json.dumps(accepted["state"], ensure_ascii=False))
            changed_foundation["questionnaire_state"]["answers"]["0-0"]["text"] = "已更新的经营事实"
            stale = digital_ip.patch_project("owner", project["id"], {
                "revision": accepted["revision"], "state": changed_foundation,
            })
            self.assertEqual(stale["foundation_stage"]["status"], "stale")
            stale_edit = json.loads(json.dumps(stale["state"], ensure_ascii=False))
            stale_edit["questionnaire_state"]["answers"]["0-0"]["text"] = "再次更新的经营事实"
            stale = digital_ip.patch_project("owner", project["id"], {
                "revision": stale["revision"], "state": stale_edit,
            })
            self.assertEqual(stale["foundation_stage"]["status"], "stale")
            changed_foundation = json.loads(json.dumps(stale["state"], ensure_ascii=False))
            changed_foundation["questionnaire_state"]["answers"]["4-1"] = {"text": "不应绕过"}
            with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "确认未变更"):
                digital_ip.patch_project("owner", project["id"], {
                    "revision": stale["revision"], "state": changed_foundation,
                })

    def test_legacy_report_does_not_unlock_content_modules(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = self._project()
            state = project["state"]
            state[digital_ip.REPORT_STATE_KEY] = {
                "report_id": "legacy", "project_revision": project["revision"], "content": _report(),
            }
            with closing(digital_ip._project_db()) as conn:
                conn.execute(
                    "UPDATE digital_ip_projects SET state_json=? WHERE id=?",
                    (json.dumps(state, ensure_ascii=False), project["id"]),
                )
                conn.commit()
            loaded = digital_ip.get_project("owner", project["id"])
            self.assertEqual(loaded["foundation_stage"]["status"], "legacy")
            loaded["state"]["questionnaire_state"]["answers"]["5-0"] = {"text": "不能填写"}
            with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "确认未变更"):
                digital_ip.patch_project("owner", project["id"], {
                    "revision": loaded["revision"], "state": loaded["state"],
                })

    def test_legacy_questionnaire_can_migrate_without_unlocking_content_modules(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = digital_ip.create_project("owner", {})
            legacy_state = {"questionnaire_state": {"answers": {"4-0": {"text": "旧版内容"}}}}
            with closing(digital_ip._project_db()) as conn:
                conn.execute(
                    "UPDATE digital_ip_projects SET state_json=? WHERE id=? AND username=?",
                    (json.dumps(legacy_state, ensure_ascii=False), project["id"], "owner"),
                )
                conn.commit()
            current = digital_ip.get_project("owner", project["id"])
            unchanged_migration = {"questionnaire_state": {
                "interviewVersion": 2, "moduleIndex": 4, "stepIndex": 0,
                "answers": {"4-0": {"text": "旧版内容"}},
            }}
            with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "确认未变更"):
                digital_ip.patch_project("owner", project["id"], {
                    "revision": current["revision"], "state": unchanged_migration,
                })
            forged_migration = {"questionnaire_state": {
                "interviewVersion": 2, "answers": {"4-0": {"text": "试图绕过关卡"}},
            }}
            with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "确认未变更"):
                digital_ip.patch_project("owner", project["id"], {
                    "revision": current["revision"], "state": forged_migration,
                })
            migrated = digital_ip.patch_project("owner", project["id"], {
                "revision": current["revision"],
                "state": {"questionnaire_state": {
                    "interviewVersion": 2, "moduleIndex": 0, "stepIndex": 0,
                    "answers": {"0-0": {"text": "唐老师", "confirmed": True}},
                    "completedModules": [], "profile": {}, "guideTurns": [],
                }},
            })
            questionnaire = migrated["state"]["questionnaire_state"]
            self.assertEqual(questionnaire["interviewVersion"], 2)
            self.assertNotIn("4-0", questionnaire["answers"])
            blocked = json.loads(json.dumps(migrated["state"], ensure_ascii=False))
            blocked["questionnaire_state"]["answers"]["4-0"] = {"text": "新版内容"}
            with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "确认未变更"):
                digital_ip.patch_project("owner", project["id"], {
                    "revision": migrated["revision"], "state": blocked,
                })

    def test_report_source_ignores_module_five_and_six_attachments(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = self._project()
            evidence = [
                {"source_ref": "answer:0-0:attachment:1", "evidence": "底座资料"},
                {"source_ref": "answer:4-0:attachment:1", "evidence": "内容资料"},
            ]
            with closing(digital_ip._project_db()) as conn:
                conn.execute(
                    "UPDATE digital_ip_projects SET confirmed_json=? WHERE id=?",
                    (json.dumps({"attachment_evidence": evidence}, ensure_ascii=False), project["id"]),
                )
                conn.commit()
            source = digital_ip._report_source(digital_ip._owned_project("owner", project["id"]))
            self.assertEqual(source["confirmed_attachment_evidence"], evidence[:1])

    def test_report_route_is_membership_gated(self):
        class Handler:
            path = "/api/gen/digital-ip/projects/project-id/report"
            headers = {"Content-Length": "14"}
            sent = None

            def _token(self): return "token"
            def _json_body_strict(self): return {"revision": 1, "consent": True}
            def _send(self, status, body): self.sent = (status, body)

        handler = Handler()
        user = {"username": "owner", "_membership_enforcement_enabled": True, "membership_active": False}
        self.assertTrue(digital_ip.dispatch_http(handler, "POST", lambda _: user, lambda _: False))
        self.assertEqual(handler.sent[0], 403)
        self.assertEqual(handler.sent[1]["code"], "membership_required")

    def test_report_rate_limit_blocks_third_request_in_a_minute(self):
        digital_ip._check_report_rate_limit("owner")
        digital_ip._check_report_rate_limit("owner")
        with self.assertRaisesRegex(digital_ip.DigitalIPRateLimited, "一分钟后"):
            digital_ip._check_report_rate_limit("owner")


if __name__ == "__main__":
    unittest.main()
