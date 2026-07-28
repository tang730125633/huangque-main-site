import base64
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import digital_ip


def _analysis():
    return {
        "summary": "这是一家有专业基础、但获客和复购不稳定的皮肤管理店。",
        "confirmed_facts": ["经营 7 年", "有 2 家门店"],
        "inferred_signals": ["老板具备长期内容素材"],
        "business_pains": [
            {"label": "获客成本高", "evidence": "平台流量越来越贵", "impact": "到店不稳定"},
        ],
        "positioning_candidates": [
            {
                "title": "问题肌管理主理人",
                "one_liner": "不制造焦虑，讲清长期改善。",
                "reasons": ["有真实经营经验"],
                "risks": ["需要持续案例"],
                "content_angles": ["顾客误区"],
            },
            {
                "title": "美业复购教练",
                "one_liner": "把一次成交变成长期关系。",
                "reasons": ["擅长老客维护"],
                "risks": ["同行受众更窄"],
                "content_angles": ["复购流程"],
            },
            {
                "title": "七年美业老板复盘者",
                "one_liner": "公开讲门店经营的得与失。",
                "reasons": ["经营经历可验证"],
                "risks": ["需要披露真实失败"],
                "content_angles": ["经营复盘"],
            },
        ],
        "recommended_index": 0,
        "follow_up_question": "老客复购下降最明显的是哪个项目？",
        "ready_to_confirm": False,
        "uncertainty_note": "还缺少具体复购数据。",
    }


def _guide_reply():
    return {
        "intent": "fill_help",
        "reply": "明白了，我们只补一个最关键的信息。",
        "follow_up_questions": ["现在最难的是获客、成交还是复购？也可以说其他。"],
        "suggested_answer": "我的门店经营了 7 年，现在最头疼的是老客复购下降。",
        "recommended_actions": [
            {"type": "fill_answer", "label": "带入回答草稿", "value": "我的门店经营了 7 年。"},
            {"type": "run_diagnosis", "label": "检查后做本步诊断", "value": ""},
        ],
        "needs_diagnosis": False,
        "uncertainty_note": "还缺少门店规模。",
    }


def _project_analysis():
    result = _analysis()
    result.update({
        "source_evidence": [{"claim": "经营 7 年", "evidence": "用户当前回答", "file_name": "用户当前回答", "location": "未定位"}],
        "gaps": ["缺少复购数据"], "conflicts": [],
        "image_plan": {"goal": "建立可信头像", "prompt": "真实门店主理人半身像", "references_needed": ["本人照片"], "steps": ["确认风格", "前往作图工具"]},
        "video_plan": {"goal": "首支口播", "format": "竖版口播", "duration_seconds": 60, "shots": ["开场", "观点", "结尾"], "steps": ["确认脚本", "前往视频工具"]},
        "next_steps": ["确认一套候选", "准备首条内容"],
    })
    return result


class DigitalIPTests(unittest.TestCase):
    def setUp(self):
        digital_ip._recent_requests.clear()
        digital_ip._guide_recent_requests.clear()
        digital_ip._guide_daily_requests.clear()
        digital_ip._project_daily_requests.clear()
        digital_ip._guide_cache.clear()
        digital_ip._project_inflight.clear()
        digital_ip._project_actions.clear()
        digital_ip._project_mutations.clear()

    def test_diagnose_uses_responses_structured_outputs(self):
        captured = {}

        def fake_post(path, body, content_type, timeout):
            captured.update(
                path=path,
                body=json.loads(body),
                content_type=content_type,
                timeout=timeout,
            )
            return {
                "model": "gpt-5.6-sol-2026-07-01",
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(_analysis(), ensure_ascii=False)}],
                }],
                "usage": {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300},
            }

        with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                mock.patch.object(digital_ip, "_post", side_effect=fake_post):
            result = digital_ip.diagnose({
                "module": "定位诊断",
                "step": "采集门店经营底图",
                "answer": "经营 7 年，平台流量越来越贵，老客复购下降。",
                "confirmed_context": [],
                "consent": True,
            }, "beauty-owner")

        self.assertEqual(captured["path"], "/v1/responses")
        self.assertEqual(captured["body"]["text"]["format"]["type"], "json_schema")
        self.assertTrue(captured["body"]["text"]["format"]["strict"])
        self.assertFalse(captured["body"]["store"])
        self.assertEqual(captured["body"]["model"], "gpt-5.6-sol")
        self.assertNotIn("beauty-owner", json.dumps(captured["body"], ensure_ascii=False))
        sent = json.loads(captured["body"]["input"])
        self.assertEqual(sent["module_rule"], digital_ip.MODULE_PROMPT_RULES[0])
        self.assertNotIn("industry_preset", sent)
        self.assertEqual(result["analysis"]["recommended_index"], 0)
        self.assertNotIn("model", result)
        self.assertTrue(result["ai_recommendation"])
        self.assertFalse(result["user_confirmed"])

    def test_payload_validation_bounds_user_input(self):
        with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "当前回答不能为空"):
            digital_ip.validate_payload({"module": "定位诊断", "step": "第一步", "answer": ""})
        with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "6000"):
            digital_ip.validate_payload({
                "module": "定位诊断",
                "step": "第一步",
                "answer": "美" * 6001,
            })

    def test_only_the_first_six_module_agents_are_available(self):
        self.assertEqual(len(digital_ip.MODULE_PROMPT_RULES), 6)
        self.assertTrue(all(digital_ip.MODULE_PROMPT_RULES))
        self.assertEqual(
            [digital_ip._module_rule(name) for name in digital_ip.ACTIVE_MODULE_NAMES],
            list(digital_ip.MODULE_PROMPT_RULES),
        )
        for module in ("朋友圈运营", "模块 9", "公众号运营", "任意新模块"):
            with self.subTest(module=module), \
                    mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                    mock.patch.object(digital_ip, "_post") as post:
                with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "开发中"):
                    digital_ip.diagnose({
                        "module": module, "step": "第一步", "answer": "开始",
                        "confirmed_context": [], "consent": True,
                    }, "module-guard")
                with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "开发中"):
                    digital_ip.guide({
                        "module": module, "step": "第一步",
                        "message": "开始", "consent": True,
                    }, "module-guard")
                post.assert_not_called()
        with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "问卷步骤无效"):
            digital_ip._clean_analysis_payload({
                "revision": 0, "module_index": 8, "step_index": 0,
                "answer": "绕过页面", "context": {}, "consent": True,
            })

    def test_answer_snapshot_ignores_client_metadata_but_detects_text_or_choice_changes(self):
        analysis_input = {"module_index": 1, "step_index": 1}
        previous = {"questionnaire_state": {"answers": {"1-1": {
            "text": "经营 7 年", "confirmed": False,
        }}}}
        metadata_only = {"questionnaire_state": {"answers": {"1-1": {
            "text": "经营 7 年", "confirmed": True, "confirmedValue": "经营 7 年",
            "aiChoice": 2, "reviewed": True,
        }}}}
        self.assertFalse(digital_ip._confirmed_answers_changed(previous, metadata_only, analysis_input))
        changed_text = {"questionnaire_state": {"answers": {"1-1": {
            "text": "经营 8 年", "confirmed": True,
        }}}}
        self.assertTrue(digital_ip._confirmed_answers_changed(previous, changed_text, analysis_input))
        confirmed_choice = {"questionnaire_state": {"answers": {"1-1": {
            "choice": ["复购"], "confirmed": True,
        }}}}
        changed_choice = {"questionnaire_state": {"answers": {"1-1": {
            "choice": ["获客"], "confirmed": True,
        }}}}
        self.assertTrue(digital_ip._confirmed_answers_changed(confirmed_choice, changed_choice, analysis_input))

    def test_refusal_is_not_treated_as_schema_output(self):
        with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "暂时无法分析"):
            digital_ip._extract_output({
                "output": [{
                    "type": "message",
                    "content": [{"type": "refusal", "refusal": "cannot comply"}],
                }],
            })
        with self.assertRaisesRegex(digital_ip.DigitalIPError, "未完成"):
            digital_ip._extract_output({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}})

    def test_rate_limit_blocks_seventh_request(self):
        for _ in range(6):
            digital_ip._check_rate_limit("owner")
        with self.assertRaises(digital_ip.DigitalIPRateLimited):
            digital_ip._check_rate_limit("owner")

    def test_guide_bounds_context_and_returns_allowlisted_actions(self):
        captured = {}

        def fake_post(path, body, content_type, timeout):
            captured.update(path=path, body=json.loads(body), timeout=timeout)
            return {
                "model": "gpt-5.6-terra",
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(_guide_reply(), ensure_ascii=False)}],
                }],
                "usage": {"input_tokens": 200, "output_tokens": 300, "total_tokens": 500},
            }

        with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                mock.patch.object(digital_ip, "_post", side_effect=fake_post):
            result = digital_ip.guide({
                "module": "定位诊断",
                "step": "采集门店经营底图",
                "step_instruction": "描述门店经营情况",
                "step_why": "建立真实底图",
                "current_answer": "美" * 2000,
                "ip_summary": "经营资料" * 300,
                "next_step": "识别核心经营痛点",
                "message": "我不知道怎么填",
                "consent": True,
                "recent_turns": [
                    {"role": "user", "content": "第 1 轮"},
                    {"role": "assistant", "content": "第 2 轮"},
                    {"role": "user", "content": "第 3 轮"},
                    {"role": "assistant", "content": "第 4 轮"},
                    {"role": "user", "content": "第 5 轮"},
                    {"role": "assistant", "content": "第 6 轮"},
                    {"role": "user", "content": "第 7 轮"},
                ],
            }, "beauty-owner")

        sent = json.loads(captured["body"]["input"])
        self.assertEqual(captured["path"], "/v1/responses")
        self.assertEqual(captured["body"]["model"], "gpt-5.6-terra")
        self.assertLessEqual(captured["body"]["max_output_tokens"], 800)
        self.assertFalse(captured["body"]["store"])
        self.assertEqual(len(sent["recent_turns"]), 6)
        self.assertEqual(sent["recent_turns"][0]["content"], "第 2 轮")
        self.assertEqual(len(sent["current_answer"]), 1200)
        self.assertEqual(len(sent["ip_summary"]), 800)
        self.assertNotIn("beauty-owner", json.dumps(captured["body"], ensure_ascii=False))
        self.assertIn("真人 IP 咨询师", captured["body"]["instructions"])
        self.assertIn("2–3 个短确认点", captured["body"]["instructions"])
        self.assertIn("2–4 个可直接选择的方向", captured["body"]["instructions"])
        self.assertIn("用户已经说过的信息不得重复询问", captured["body"]["instructions"])
        self.assertIn("绝不向用户提及报告、表格、字段", captured["body"]["instructions"])
        self.assertEqual(result["guide"]["recommended_actions"][0]["type"], "fill_answer")
        self.assertEqual(len(result["guide"]["follow_up_questions"]), 1)
        self.assertNotIn("model", result)
        self.assertTrue(result["guide_only"])
        self.assertFalse(result["user_confirmed"])

    def test_guide_cache_avoids_a_second_model_call(self):
        response = {
            "model": "gpt-5.6-terra",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(_guide_reply(), ensure_ascii=False)}],
            }],
        }
        payload = {
            "module": "定位诊断",
            "step": "经营底图",
            "message": "请用简单的话问我",
            "consent": True,
        }
        with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                mock.patch.object(digital_ip, "_post", return_value=response) as post:
            self.assertFalse(digital_ip.guide(payload, "owner")["cached"])
            self.assertTrue(digital_ip.guide(payload, "owner")["cached"])
        self.assertEqual(post.call_count, 1)

    def test_guide_output_hard_stops_double_questions_and_internal_terms(self):
        unsafe = _guide_reply()
        unsafe.update({
            "reply": "为了补齐字段，你现在最想解决什么？",
            "follow_up_questions": ["请先看采集表好吗？你更在意获客还是成交？"],
            "suggested_answer": "报告字段：最看重获客",
            "recommended_actions": [
                {"type": "fill_answer", "label": "填入表格", "value": "报告里写获客"},
            ],
        })
        result = digital_ip._extract_guide_output({
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(unsafe, ensure_ascii=False)}],
            }],
        })
        self.assertEqual(result["reply"], "我明白了，我们继续把这一点说清楚。")
        self.assertEqual(len(result["follow_up_questions"]), 1)
        question = result["follow_up_questions"][0]
        self.assertEqual(question.count("？") + question.count("?"), 1)
        self.assertFalse(any(term in result["reply"] + question for term in digital_ip.GUIDE_INTERNAL_TERMS))
        self.assertEqual(result["suggested_answer"], "")
        self.assertEqual(result["recommended_actions"], [])

    def test_guide_rate_limit_allows_normal_interview_burst(self):
        self.assertEqual(12, digital_ip.GUIDE_RATE_LIMIT_PER_MINUTE)
        self.assertGreaterEqual(digital_ip.GUIDE_DAILY_LIMIT, sum(digital_ip.PROJECT_MODULE_STEPS) * 2)
        for _ in range(digital_ip.GUIDE_RATE_LIMIT_PER_MINUTE):
            digital_ip._check_guide_rate_limit("owner")
        with self.assertRaises(digital_ip.DigitalIPRateLimited):
            digital_ip._check_guide_rate_limit("owner")

    def test_legacy_diagnose_and_guide_require_explicit_consent(self):
        with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                mock.patch.object(digital_ip, "_post") as post:
            with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "明确同意"):
                digital_ip.diagnose({"module": "定位诊断", "step": "经营底图", "answer": "测试回答"}, "owner")
            with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "明确同意"):
                digital_ip.guide({"module": "定位诊断", "step": "经营底图", "message": "怎么填写"}, "owner")
        post.assert_not_called()

    def test_structured_output_failure_releases_legacy_quotas(self):
        incomplete = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
        with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                mock.patch.object(digital_ip, "_post", return_value=incomplete):
            with self.assertRaises(digital_ip.DigitalIPError):
                digital_ip.diagnose({
                    "module": "定位诊断", "step": "经营底图", "answer": "门店经营七年", "consent": True,
                }, "owner")
            with self.assertRaises(digital_ip.DigitalIPError):
                digital_ip.guide({
                    "module": "定位诊断", "step": "经营底图", "message": "请帮我理解", "consent": True,
                }, "owner")
        self.assertNotIn("owner", digital_ip._recent_requests)
        self.assertNotIn("owner", digital_ip._guide_recent_requests)
        self.assertFalse(digital_ip._guide_daily_requests)

    def test_project_owner_cas_and_no_raw_file_persistence(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = digital_ip.create_project("owner", {"title": "美业 IP"})
            self.assertEqual(project["status"], "draft")
            self.assertEqual(digital_ip.list_projects("other"), [])
            with self.assertRaises(digital_ip.DigitalIPNotFound):
                digital_ip.get_project("other", project["id"])
            updated = digital_ip.patch_project("owner", project["id"], {
                "revision": project["revision"], "state": {"questionnaire_state": {"answers": {"1-1": {"text": "经营 7 年"}}, "profile": {"1": {"summary": "可信主理人"}}}},
            })
            self.assertEqual(updated["state"]["questionnaire_state"]["profile"]["1"]["summary"], "可信主理人")
            with self.assertRaises(digital_ip.DigitalIPRevisionConflict):
                digital_ip.patch_project("owner", project["id"], {"revision": project["revision"], "title": "旧标签"})
            encoded = base64.b64encode(b"photo").decode()
            analysis_data = _project_analysis()
            analysis_data["source_evidence"].append({
                "claim": "主理人照片", "evidence": "photo", "file_name": "me.png", "location": "图片区域",
            })
            response = {"model": "test", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(analysis_data, ensure_ascii=False)}]}]}
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), mock.patch.object(digital_ip, "_post", return_value=response) as post:
                result = digital_ip.analyze_project("owner", project["id"], {
                    "revision": updated["revision"], "module_index": 1, "step_index": 1,
                    "answer": "经营 7 年", "files": [{"name": "me.png", "type": "image/png", "data_url": "data:image/png;base64," + encoded}], "consent": True,
                })
            self.assertEqual(len(result["analysis"]["positioning_candidates"]), 3)
            self.assertNotIn("model", result)
            self.assertNotIn("model", result["project"]["last_analysis"])
            self.assertEqual(result["project"]["status"], "candidate_ready")
            self.assertEqual(json.loads(post.call_args.args[1])["max_output_tokens"], 25000)
            self.assertEqual(json.loads(post.call_args.args[1])["text"]["verbosity"], "low")
            request = json.loads(post.call_args.args[1])
            self.assertEqual(
                json.loads(request["input"][0]["content"][0]["text"])["module_rule"],
                digital_ip.MODULE_PROMPT_RULES[1],
            )
            self.assertIn(digital_ip.MODULE_PROMPT_RULES[1], request["instructions"])
            content = json.loads(post.call_args.args[1])["input"][0]["content"]
            self.assertEqual(next(item for item in content if item["type"] == "input_image")["detail"], "high")
            with closing(digital_ip._project_db()) as conn:
                stored = conn.execute("SELECT last_analysis_json FROM digital_ip_projects").fetchone()[0]
            self.assertNotIn(encoded, stored)
            confirmed = digital_ip.confirm_project("owner", project["id"], {
                "revision": result["project"]["revision"], "candidate_index": 1, "answer": "被篡改的回答",
            })
            self.assertEqual(confirmed["project"]["confirmed_profile"]["title"], _project_analysis()["positioning_candidates"][1]["title"])
            self.assertEqual(confirmed["project"]["status"], "confirmed")
            with closing(digital_ip._project_db()) as conn:
                confirmed_json = conn.execute("SELECT confirmed_json FROM digital_ip_projects").fetchone()[0]
            self.assertIn("经营 7 年", confirmed_json)
            self.assertNotIn("被篡改的回答", confirmed_json)
            preserved = digital_ip.patch_project("owner", project["id"], {
                "revision": confirmed["project"]["revision"],
                "state": {"questionnaire_state": {"answers": {"1-1": {
                    "text": "经营 7 年", "confirmed": True, "confirmedValue": "经营 7 年",
                    "aiChoice": 1, "reviewed": True,
                }}}},
            })
            self.assertEqual(preserved["status"], "confirmed")
            self.assertEqual(preserved["confirmed_profile"]["title"], analysis_data["positioning_candidates"][1]["title"])
            self.assertEqual(preserved["confirmed_plans"]["image_plan"], analysis_data["image_plan"])
            self.assertEqual(
                digital_ip._report_source(digital_ip._owned_project("owner", project["id"]))["confirmed_attachment_evidence"][0]["file_name"],
                "me.png",
            )
            skipped = digital_ip.patch_project("owner", project["id"], {
                "revision": preserved["revision"],
                "state": {"questionnaire_state": {"answers": {"1-1": {"text": "经营 7 年", "confirmed": False, "skipped": True}}}},
            })
            self.assertEqual(skipped["status"], "draft")
            self.assertNotIn("last_analysis", skipped)
            self.assertNotIn("confirmed_profile", skipped)
            self.assertEqual(
                digital_ip._report_source(digital_ip._owned_project("owner", project["id"]))["confirmed_attachment_evidence"],
                [],
            )
            next_draft = digital_ip.patch_project("owner", project["id"], {
                "revision": skipped["revision"], "state": {"questionnaire_state": {"answers": {"0-0": {"text": "更新后的经营资料"}}}},
            })
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), mock.patch.object(digital_ip, "_post", return_value=response):
                newer = digital_ip.analyze_project("owner", project["id"], {
                    "revision": next_draft["revision"], "module_index": 0, "step_index": 0, "answer": "更新后的经营资料", "consent": True,
                })
            self.assertEqual(newer["project"]["status"], "candidate_ready")
            changed = digital_ip.patch_project("owner", project["id"], {
                "revision": newer["project"]["revision"], "state": {"questionnaire_state": {"answers": {"0-0": {"text": "分析后又修改的回答", "confirmed": True}}}},
            })
            with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "有效分析"):
                digital_ip.confirm_project("owner", project["id"], {"revision": changed["revision"], "candidate_index": 0})

    def test_project_validation_failure_does_not_change_revision(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = digital_ip.create_project("owner", {})
            project = digital_ip.patch_project("owner", project["id"], {
                "revision": project["revision"], "state": {"questionnaire_state": {"answers": {"1-1": {"text": "经营 7 年"}}}},
            })
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), mock.patch.object(digital_ip, "_post", side_effect=OSError("offline")):
                with self.assertRaises(digital_ip.DigitalIPError):
                    digital_ip.analyze_project("owner", project["id"], {
                        "revision": project["revision"], "module_index": 1, "step_index": 1, "answer": "经营 7 年", "consent": True,
                    })
            self.assertEqual(digital_ip.get_project("owner", project["id"])["revision"], project["revision"])
            self.assertNotIn("owner", digital_ip._recent_requests)
            self.assertFalse(digital_ip._project_daily_requests)
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                    mock.patch.object(digital_ip, "_post", return_value={"status": "incomplete"}):
                with self.assertRaises(digital_ip.DigitalIPError):
                    digital_ip.analyze_project("owner", project["id"], {
                        "revision": project["revision"], "module_index": 1, "step_index": 1,
                        "answer": "经营 7 年", "consent": True,
                    })
            self.assertNotIn("owner", digital_ip._recent_requests)
            self.assertFalse(digital_ip._project_daily_requests)
            with self.assertRaises(digital_ip.DigitalIPValidationError):
                digital_ip._clean_project_files([{ "name": "bad.exe", "type": "application/octet-stream", "data_url": "data:application/octet-stream;base64,AA==" }])

    def test_project_mutation_is_rejected_while_ai_action_runs(self):
        entered, release = threading.Event(), threading.Event()
        response = {"model": "test", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(_project_analysis(), ensure_ascii=False)},
        ]}]}

        def slow_post(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return response

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = digital_ip.create_project("owner", {})
            project = digital_ip.patch_project("owner", project["id"], {
                "revision": project["revision"], "state": {"questionnaire_state": {"answers": {"1-1": {"text": "经营 7 年"}}}},
            })
            payload = {"revision": project["revision"], "module_index": 1, "step_index": 1, "answer": "经营 7 年", "consent": True}
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                    mock.patch.object(digital_ip, "_post", side_effect=slow_post), \
                    ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(digital_ip.analyze_project, "owner", project["id"], payload)
                self.assertTrue(entered.wait(2))
                with self.assertRaises(digital_ip.DigitalIPRevisionConflict):
                    digital_ip.patch_project("owner", project["id"], {"revision": project["revision"], "title": "不应写入"})
                with self.assertRaises(digital_ip.DigitalIPRevisionConflict):
                    digital_ip.confirm_project("owner", project["id"], {"revision": project["revision"], "candidate_index": 0})
                release.set()
                self.assertTrue(future.result()["ok"])
            self.assertEqual(digital_ip.get_project("owner", project["id"])["title"], "未命名数字 IP")

    def test_review_step_can_be_analyzed_and_confirmed(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = digital_ip.create_project("owner", {})
            review_text = "专业文章：方法与边界。\n故事文章：顾客转折。"
            project = digital_ip.patch_project("owner", project["id"], {
                "revision": project["revision"], "state": {"questionnaire_state": {"answers": {"3-1": {"text": review_text}}}},
            })
            response = {"model": "test", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(_project_analysis(), ensure_ascii=False)}]}]}
            with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), mock.patch.object(digital_ip, "_post", return_value=response):
                result = digital_ip.analyze_project("owner", project["id"], {
                    "revision": project["revision"], "module_index": 3, "step_index": 1,
                    "answer": review_text, "consent": True,
                })
            confirmed = digital_ip.confirm_project("owner", project["id"], {
                "revision": result["project"]["revision"], "candidate_index": 0,
            })
            self.assertEqual(confirmed["project"]["status"], "confirmed")

    def test_existing_project_db_permissions_are_tightened(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ip.db"
            path.touch(mode=0o644)
            with mock.patch.object(digital_ip, "PROJECT_DB", path), closing(digital_ip._project_db()):
                pass
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_project_db_wal_and_shared_memory_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ip.db"
            with mock.patch.object(digital_ip, "PROJECT_DB", path), closing(digital_ip._project_db()) as conn:
                conn.execute("INSERT INTO digital_ip_projects(id,username,title,created_at,updated_at) VALUES('1','u','t',1,1)")
                conn.commit()
                for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                    self.assertTrue(candidate.exists())
                    self.assertEqual(candidate.stat().st_mode & 0o777, 0o600)

    def test_model_echoed_data_urls_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            project = digital_ip.create_project("owner", {})
            project = digital_ip.patch_project("owner", project["id"], {
                "revision": project["revision"], "state": {"questionnaire_state": {"answers": {"0-0": {"text": "经营 7 年"}}}},
            })
            for leaked in (
                "data:image/png;base64,c2VjcmV0",
                "data:text/plain,secret",
                "data:image/png;charset=utf-8;base64,c2VjcmV0",
            ):
                with self.subTest(leaked=leaked):
                    analysis = _project_analysis()
                    analysis["summary"] = "模型回显 " + leaked
                    response = {"model": "internal-model", "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": json.dumps(analysis, ensure_ascii=False)},
                    ]}]}
                    with mock.patch.object(digital_ip, "OPENAI_KEY", "configured"), \
                            mock.patch.object(digital_ip, "_post", return_value=response), \
                            self.assertRaisesRegex(digital_ip.DigitalIPError, "不可保存"):
                        digital_ip.analyze_project("owner", project["id"], {
                            "revision": project["revision"], "module_index": 0, "step_index": 0,
                            "answer": "经营 7 年", "consent": True,
                        })
                    self.assertEqual(digital_ip.get_project("owner", project["id"])["revision"], project["revision"])
                    self.assertNotIn("owner", digital_ip._recent_requests)
                    self.assertFalse(digital_ip._project_daily_requests)

    def test_project_table_initializes_concurrently_without_touching_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "content_jobs.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO jobs(value) VALUES('kept')")
            with mock.patch.object(digital_ip, "PROJECT_DB", path), ThreadPoolExecutor(max_workers=4) as pool:
                projects = list(pool.map(lambda index: digital_ip.create_project("owner-%d" % index, {}), range(4)))
            self.assertEqual(len(projects), 4)
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("SELECT value FROM jobs").fetchone()[0], "kept")

    def test_project_daily_limit_blocks_thirteenth_request(self):
        for _ in range(12):
            digital_ip._check_project_daily_limit("owner")
        with self.assertRaisesRegex(digital_ip.DigitalIPRateLimited, "今日分析次数已用完"):
            digital_ip._check_project_daily_limit("owner")

    def test_project_creation_is_limited_per_user(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(digital_ip, "PROJECT_DB", Path(directory) / "ip.db"):
            for _ in range(digital_ip.MAX_PROJECTS_PER_USER):
                digital_ip.create_project("owner", {})
            with self.assertRaisesRegex(digital_ip.DigitalIPValidationError, "最多保留"):
                digital_ip.create_project("owner", {})

    def test_http_adapter_blocks_paid_analysis_for_inactive_member(self):
        class Handler:
            path = "/api/gen/digital-ip/projects/project-id/analyze"
            headers = {"Content-Length": "2"}
            sent = None

            def _token(self): return "token"
            def _json_body_strict(self): return {}
            def _send(self, status, body): self.sent = (status, body)

        handler = Handler()
        user = {"username": "owner", "_membership_enforcement_enabled": True, "membership_active": False}
        self.assertTrue(digital_ip.dispatch_http(handler, "POST", lambda _: user, lambda _: False))
        self.assertEqual(handler.sent[0], 403)
        self.assertEqual(handler.sent[1]["code"], "membership_required")


if __name__ == "__main__":
    unittest.main()
