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
    for module_index, step_count in enumerate(digital_ip.PROJECT_MODULE_STEPS):
        for step_index in range(step_count):
            answers["%d-%d" % (module_index, step_index)] = {
                "text": "获客成本高，老客复购下降" if (module_index, step_index) == (0, 0) else "",
                "confirmed": (module_index, step_index) == (0, 0),
                "skipped": (module_index, step_index) != (0, 0),
            }
    return {"questionnaire_state": {"answers": answers, "profile": {"定位": "真实门店经营者"}}}


def _report(product_id="script_studio"):
    return {
        "title": "美业数字化 IP 产品方案",
        "executive_summary": "先以真实经营问题建立内容可信度，再验证复购改善路径。",
        "evidence": [{
            "evidence_id": "E1",
            "claim": "门店面临获客与复购压力",
            "source_ref": "answer:0-0",
            "source_excerpt": "获客成本高，老客复购下降",
        }],
        "industry_pains": [{
            "pain": "获客与复购不稳定",
            "evidence_ids": ["E1"],
            "why_it_matters": "内容需先回应真实经营问题。",
            "product_matches": [{
                "product_id": product_id,
                "fit_reason": "可先把已确认问题整理为可拍脚本。",
                "execution_steps": ["核对事实边界", "生成脚本候选", "用户审阅后再拍摄"],
            }],
        }],
        "execution_plan": [{
            "phase": "第一阶段", "goal": "验证内容方向",
            "steps": ["整理事实", "生成脚本候选", "人工确认"],
        }],
        "metrics": [{
            "name": "内容咨询率", "definition": "内容带来的有效咨询数/内容触达数",
            "baseline": "待确认", "target": "由用户完成首轮记录后确认",
            "review_cycle": "每周", "evidence_ids": ["E1"],
        }],
        "material_gaps": [{
            "gap": "其余 53 步未提供资料", "why_needed": "限制更细的产品匹配",
            "how_to_collect": "回到项目补充被跳过步骤", "blocking": False,
            "source_refs": [
                "answer:%d-%d" % (module_index, step_index)
                for module_index, step_count in enumerate(digital_ip.PROJECT_MODULE_STEPS)
                for step_index in range(step_count)
                if (module_index, step_index) != (0, 0)
            ],
        }],
        "disclaimer": "仅基于用户确认资料；产品可用性以页面实时状态为准，不保证经营结果。",
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

    def test_requires_all_54_steps_before_paid_model_call(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = digital_ip.create_project("owner", {})
            with mock.patch.object(digital_ip, "_post") as post, \
                    self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "全部 54 步"):
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
            self.assertEqual(prompt["confirmed_answers"], [{"source_ref": "answer:0-0", "answer": "获客成本高，老客复购下降"}])
            self.assertEqual(len(prompt["skipped_steps"]), 53)
            self.assertEqual({item["id"] for item in prompt["product_catalog"]}, digital_ip.PRODUCT_IDS)
            self.assertFalse(result["stale"])
            self.assertEqual(result["report"]["progress"], {"total": 54, "confirmed": 1, "skipped": 53, "unresolved": 0})
            self.assertEqual(digital_ip.get_report("owner", project["id"])["report"]["report_id"], result["report"]["report_id"])
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

    def test_provider_failure_and_invalid_product_do_not_persist(self):
        invalid = _report(product_id="invented_product")
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
        self.assertEqual(result["report"]["content"]["evidence"][0]["source_location"], "问卷步骤 0-0")
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
