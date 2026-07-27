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


def _content():
    return {
        "title": "OpenAI GPT-4o 门店 IP 方案",
        "executive_summary": "用真实资料建立可信内容。",
        "evidence": [{
            "evidence_id": "E1", "claim": "复购下降<script>",
            "source_excerpt": "老客复购下降", "source_name": "经营资料.pdf", "source_location": "第 2 页",
        }],
        "industry_pains": [{
            "pain": "复购不足", "why_it_matters": "影响长期增长", "evidence_ids": ["E1"],
            "product_matches": [{
                "product_id": "image_studio", "fit_reason": "建立统一视觉", "execution_steps": ["确认提示词"],
            }],
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


def _payload(stale=True):
    return {
        "project": {"id": "ip12-1", "title": "我的门店 IP", "revision": 3},
        "report": {
            "report_id": "report-1", "generated_at": 1785150000,
            "progress": {"total": 54, "confirmed": 53, "skipped": 1}, "content": _content(),
        },
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
        project = digital_ip.create_project(owner, {"title": "我的门店 IP"})
        envelope = _payload(False)["report"]
        envelope["source_revision"] = project["revision"]
        envelope["project_revision"] = project["revision"]
        with digital_ip._project_db() as connection:
            connection.execute(
                "UPDATE digital_ip_projects SET state_json=? WHERE id=? AND username=?",
                (json.dumps({digital_ip.REPORT_STATE_KEY: envelope}, ensure_ascii=False), project["id"], owner),
            )
            connection.commit()
        return project

    def test_html_is_polished_escaped_neutral_and_linked(self):
        document = ip12_pdf.build_report_html(_payload())
        self.assertIn("真实资料", document)
        self.assertIn("本 PDF 是历史报告快照", document)
        self.assertIn("https://huangquechuanmei.com/workbench/banana.html", document)
        self.assertIn("可点击跳转使用我们的网站功能", document)
        self.assertIn("AI 服务 门店 IP 方案", document)
        self.assertNotIn("OpenAI", document)
        self.assertNotIn("GPT-4o", document)
        self.assertNotIn("<script>", document)
        self.assertIn("&lt;script&gt;", document)

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
