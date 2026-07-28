import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import digital_ip, ip12_pdf


def _foundation_content():
    return {
        "title": "OpenAI GPT-4o IP 定位阶段报告",
        "executive_summary": "用真实资料建立可信定位。",
        "evidence": [{
            "evidence_id": "E1", "claim": "复购下降<script>",
            "source_excerpt": "老客复购下降", "source_name": "经营资料.pdf", "source_location": "第 2 页",
        }],
        "modules": [
            {
                "module_id": module_id, "title": title, "summary": f"{title}摘要",
                "findings": [{
                    "kind": "fact", "title": "已确认资料", "detail": "仅引用已确认回答",
                    "evidence_ids": ["E1"], "risks": [],
                }],
            }
            for module_id, title in enumerate(("定位诊断", "人设塑造", "价值主张", "故事资产"), 1)
        ],
        "execution_priorities": [{
            "priority": "P0", "module_id": 1, "task": "核对定位资料", "output": "确认后的定位档案",
            "evidence_ids": ["E1"],
        }],
        "confirmation_items": [{
            "item": "公开范围", "reason": "敏感经历需本人确认", "evidence_ids": ["E1"], "required": True,
        }],
        "material_gaps": [{
            "gap": "缺少月报", "why_needed": "建立基线", "how_to_collect": "导出月报", "blocking": False,
        }],
        "disclaimer": "仅基于已确认资料，AI 推断需本人复核。",
    }


def _legacy_content():
    return {
        "title": "历史产品报告",
        "executive_summary": "用真实资料建立可信内容。",
        "evidence": [{
            "evidence_id": "E1", "claim": "复购下降", "source_excerpt": "老客复购下降",
            "source_name": "经营资料.pdf", "source_location": "第 2 页",
        }],
        "industry_pains": [{
            "pain": "复购不足", "why_it_matters": "影响长期增长", "evidence_ids": ["E1"],
            "product_matches": [],
        }],
        "execution_plan": [{"phase": "第一阶段", "goal": "验证方向", "steps": ["整理事实"]}],
        "metrics": [{
            "name": "复购率", "definition": "复购人数占比", "baseline": "待确认",
            "target": "记录后确认", "review_cycle": "每月", "evidence_ids": ["E1"],
        }],
        "material_gaps": [{
            "gap": "缺少月报", "why_needed": "建立基线", "how_to_collect": "导出月报", "blocking": False,
        }],
        "disclaimer": "仅基于已确认资料。",
    }


def _payload(stale=True, legacy=False):
    report = {
        "report_id": "report-1", "generated_at": 1785150000,
        "progress": {"total": 54 if legacy else 30, "confirmed": 53 if legacy else 30, "skipped": 1 if legacy else 0},
        "content": _legacy_content() if legacy else _foundation_content(),
    }
    if not legacy:
        report.update(stage=digital_ip.FOUNDATION_STAGE, status="confirmed")
    return {
        "project": {"id": "ip12-1", "title": "我的数字化 IP", "revision": 3},
        "report": report,
        "stale": stale,
    }


class _Handler:
    def __init__(self, path, token="token"):
        self.path = path
        self.headers = {}
        self._raw_token = token
        self.status = None
        self.response_headers = {}
        self.sent = None
        self.wfile = io.BytesIO()

    def _token(self):
        return self._raw_token

    def _send(self, status, body):
        self.sent = (status, body)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass


class IP12PDFTests(unittest.TestCase):
    def setUp(self):
        if digital_ip._pdf_lock.locked():
            digital_ip._pdf_lock.release()
        digital_ip._pdf_cache.clear()
        digital_ip._pdf_recent_renders.clear()

    def _seed_report(self, directory, owner="owner"):
        database = Path(directory) / "ip.db"
        patcher = mock.patch.object(digital_ip, "PROJECT_DB", database)
        patcher.start()
        self.addCleanup(patcher.stop)
        project = digital_ip.create_project(owner, {"title": "我的数字化 IP"})
        envelope = _payload(False)["report"]
        envelope["source_revision"] = project["revision"]
        envelope["project_revision"] = project["revision"]
        with digital_ip._project_db() as connection:
            row = connection.execute(
                "SELECT * FROM digital_ip_projects WHERE id=? AND username=?", (project["id"], owner),
            ).fetchone()
            envelope["source_hash"] = digital_ip._source_hash(digital_ip._report_source(row))
            connection.execute(
                "UPDATE digital_ip_projects SET state_json=? WHERE id=? AND username=?",
                (json.dumps({digital_ip.REPORT_STATE_KEY: envelope}, ensure_ascii=False), project["id"], owner),
            )
            connection.commit()
        return project

    def test_foundation_schema_has_four_modules_and_confirmation_items(self):
        schema = digital_ip.REPORT_SCHEMA
        self.assertIn("modules", schema["required"])
        self.assertIn("execution_priorities", schema["required"])
        self.assertIn("confirmation_items", schema["required"])
        modules = schema["properties"]["modules"]
        self.assertEqual((modules["minItems"], modules["maxItems"]), (4, 4))
        self.assertEqual(modules["items"]["properties"]["module_id"]["enum"], [1, 2, 3, 4])

    def test_foundation_html_is_polished_escaped_neutral_and_linked(self):
        document = ip12_pdf.build_report_html(_payload())
        for text in ("定位诊断", "人设塑造", "价值主张", "故事资产", "待确认事项"):
            self.assertIn(text, document)
        for heading in ("模块一｜定位诊断", "模块二｜人设塑造", "模块三｜价值主张", "模块四｜故事资产"):
            self.assertIn(heading, document)
        self.assertIn("本 PDF 是历史报告快照", document)
        self.assertIn('@top-left{content:"IP 人设定位｜模块 1–4"', document)
        self.assertIn("@bottom-right{content:counter(page)", document)
        self.assertIn("background:#fff", document)
        self.assertIn("border-bottom:3px solid #e2e5e9", document)
        self.assertIn("<table>", document)
        self.assertNotIn("class='cover'", document)
        self.assertNotIn("class='card'", document)
        self.assertNotIn("linear-gradient", document)
        self.assertIn("AI 服务", document)
        self.assertNotIn("OpenAI", document)
        self.assertNotIn("GPT-4o", document)
        self.assertNotIn("<script>", document)
        self.assertIn("&lt;script&gt;", document)

    def test_legacy_report_remains_renderable(self):
        document = ip12_pdf.build_report_html(_payload(legacy=True))
        self.assertIn("复购不足", document)
        self.assertIn("历史产品报告", document)
        self.assertNotIn("<script>", document)

    def test_real_browser_output_has_pdf_signature_when_available(self):
        browser = os.environ.get("DIGITAL_IP_PDF_TEST_BROWSER", "").strip()
        if os.environ.get("CI") and not browser:
            self.skipTest("CI browser probe requires DIGITAL_IP_PDF_TEST_BROWSER")
        browser = browser or ip12_pdf._browser_path()
        if not browser:
            self.skipTest("Chromium-compatible browser unavailable")
        output = ip12_pdf.render_report_pdf(_payload(), browser=browser)
        self.assertTrue(output.startswith(b"%PDF-"))
        self.assertGreater(len(output), 10000)

    def test_export_is_owned_and_does_not_call_model(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self._seed_report(directory)
            with mock.patch.object(ip12_pdf, "render_report_pdf", return_value=b"%PDF-test") as render, \
                    mock.patch.object(digital_ip, "_post") as post:
                data, filename = digital_ip.export_report_pdf("owner", project["id"])
                self.assertEqual(data, b"%PDF-test")
                self.assertRegex(filename, r"^huangque-ip12-[a-zA-Z0-9_-]+\.pdf$")
                self.assertIn("pdf_url", render.call_args.args[0]["report"])
                post.assert_not_called()
            with self.assertRaises(digital_ip.DigitalIPNotFound):
                digital_ip.export_report_pdf("other", project["id"])

    def test_same_report_is_cached_and_new_render_is_rate_limited(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self._seed_report(directory)
            with mock.patch.object(ip12_pdf, "render_report_pdf", return_value=b"%PDF-test") as render:
                first = digital_ip.export_report_pdf("owner", project["id"])
                second = digital_ip.export_report_pdf("owner", project["id"])
                self.assertEqual(first, second)
                render.assert_called_once()
                digital_ip._pdf_cache.clear()
                with self.assertRaises(digital_ip.DigitalIPPDFBusy):
                    digital_ip.export_report_pdf("owner", project["id"])
                render.assert_called_once()

    def test_renderer_error_releases_rate_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self._seed_report(directory)
            with mock.patch.object(ip12_pdf, "render_report_pdf", side_effect=OSError("browser failed")):
                with self.assertRaises(digital_ip.DigitalIPPDFUnavailable):
                    digital_ip.export_report_pdf("owner", project["id"])
            self.assertNotIn("owner", digital_ip._pdf_recent_renders)

    def test_pdf_route_requires_login_and_sends_private_binary(self):
        path = "/api/gen/digital-ip/projects/ip12-1/report.pdf"
        anonymous = _Handler(path, token="")
        self.assertTrue(digital_ip.dispatch_http(anonymous, "GET", lambda _token: None, lambda _user: False))
        self.assertEqual(anonymous.sent[0], 401)

        handler = _Handler(path)
        with mock.patch.object(digital_ip, "export_report_pdf", return_value=(b"%PDF-test", "huangque-ip12-r1.pdf")):
            self.assertTrue(digital_ip.dispatch_http(
                handler, "GET", lambda _token: {"username": "owner"}, lambda _user: False,
            ))
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.response_headers["Content-Type"], "application/pdf")
        self.assertEqual(handler.response_headers["Cache-Control"], "private, no-store, max-age=0")
        self.assertEqual(handler.response_headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(handler.wfile.getvalue(), b"%PDF-test")

    def test_concurrent_export_is_rejected_before_rendering(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self._seed_report(directory)
            digital_ip._pdf_lock.acquire()
            try:
                with mock.patch.object(ip12_pdf, "render_report_pdf") as render, \
                        self.assertRaises(digital_ip.DigitalIPPDFBusy):
                    digital_ip.export_report_pdf("owner", project["id"])
                render.assert_not_called()
            finally:
                digital_ip._pdf_lock.release()


if __name__ == "__main__":
    unittest.main()
