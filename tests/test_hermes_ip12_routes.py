import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
HERMES = ROOT / "server" / "hermes_ip12"


def extract_js_function(source, name):
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


class HermesIP12SourceTests(unittest.TestCase):
    def test_coach_prompt_finishes_ready_outputs_in_the_same_reply(self):
        prompt = (HERMES / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("同一条回复", prompt)
        self.assertIn("绝不说“请稍等”", prompt)
        self.assertIn("立刻执行 Step 2", prompt)
        self.assertIn("模块切换必须同步界面", prompt)

    def test_complete_original_route_set_is_present(self):
        routes = set()
        pattern = re.compile(r'(?:@app\.route|app\.add_url_rule)\(\s*["\']([^"\']+)')
        for path in HERMES.glob("*.py"):
            routes.update(pattern.findall(path.read_text(encoding="utf-8")))

        self.assertEqual(len(routes), 76)
        self.assertTrue(
            {
                "/api/chat",
                "/api/generate-report",
                "/api/generate-deliverable",
                "/api/generate-image",
                "/api/generate-video",
                "/api/foundation-report/generate",
                "/api/analyze-video",
                "/api/pipeline",
                "/api/replica",
                "/api/agnes/video",
                "/api/team-workbench/submit",
                "/classic",
                "/skills",
                "/analytics",
                "/agnes-lab",
                "/team-workbench",
            }.issubset(routes)
        )

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_inline_javascript_parses(self):
        failures = []
        for path in sorted((HERMES / "templates").glob("*.html")):
            html = path.read_text(encoding="utf-8")
            for index, script in enumerate(
                re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.I | re.S),
                1,
            ):
                if not script.strip():
                    continue
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".js", encoding="utf-8", delete=False
                ) as handle:
                    handle.write(script)
                    temp_path = handle.name
                try:
                    result = subprocess.run(
                        ["node", "--check", temp_path], capture_output=True, text=True
                    )
                finally:
                    Path(temp_path).unlink(missing_ok=True)
                if result.returncode:
                    failures.append(f"{path.name} script {index}: {result.stderr}")
        self.assertEqual(failures, [])

    def test_runtime_files_use_environment_secrets(self):
        paths = list(HERMES.glob("*.py"))
        source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        self.assertNotRegex(source, r"sk-[A-Za-z0-9_-]{12,}")
        self.assertNotRegex(source, r"ark-[A-Za-z0-9_-]{12,}")
        literal_credentials = []
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Assign):
                    names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                    if (
                        names
                        and any(mark in names[0].upper() for mark in ("KEY", "TOKEN", "SECRET"))
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                        and node.value.value
                    ):
                        literal_credentials.append((path.name, node.lineno, names[0]))
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if (
                            isinstance(key, ast.Constant)
                            and str(key.value).lower() in {"authorization", "x-api-key"}
                            and isinstance(value, ast.Constant)
                            and value.value
                        ):
                            literal_credentials.append((path.name, node.lineno, key.value))
        self.assertEqual(literal_credentials, [])
        unit = (ROOT / "deploy/systemd/hermes-ip12-preview.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("EnvironmentFile=/home/ubuntu/.secrets/hermes-openai.env", unit)
        self.assertIn("port=3102", unit)

    def test_foundation_pdf_renderer_waits_for_chromium_exit(self):
        source = (HERMES / "server.py").read_text(encoding="utf-8")
        self.assertIn("subprocess.run(", source)
        self.assertIn("timeout=60", source)
        self.assertIn("价值主张诊断表", source)
        self.assertIn("故事库（至少5个）", source)
        self.assertIn("内容资产使用表", source)
        self.assertIn("优化建议汇总", source)

    def test_security_boundaries_and_runtime_ignores_are_kept(self):
        index = (HERMES / "templates/index.html").read_text(encoding="utf-8")
        classic = (HERMES / "templates/index_clean.html").read_text(encoding="utf-8")
        skills = (HERMES / "templates/skills.html").read_text(encoding="utf-8")
        team = (HERMES / "templates/team_workbench.html").read_text(encoding="utf-8")
        agnes = (HERMES / "templates/agnes_lab.html").read_text(encoding="utf-8")
        self.assertIn("marked.parse(eHtml(t))", index)
        self.assertIn("marked.parse(escHtml(text))", classic)
        self.assertIn("marked@15.0.12/lib/marked.umd.js", index)
        self.assertIn("marked@15.0.12/lib/marked.umd.js", classic)
        self.assertIn("typeof marked!=='undefined'", classic)
        self.assertIn("sanitizeMarked(marked.parse", index)
        self.assertIn("sanitizeMarked(marked.parse", classic)
        self.assertIn("escHtml(c.title)", classic)
        self.assertNotIn("<span>${c.title}", classic)
        self.assertIn("esc(d.report)", skills)
        self.assertIn("function safeUrl(s)", team)
        self.assertIn("const safeUrl=s=>", agnes)
        self.assertIn("esc(v.prompt)", (HERMES / "templates/videos.html").read_text(encoding="utf-8"))
        self.assertIn("span.textContent = msg", (HERMES / "templates/video_factory.html").read_text(encoding="utf-8"))

        requirements = (HERMES / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("yt-dlp", requirements)
        self.assertIn("pypdf", requirements)
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for path in (
            "server/hermes_ip12/data/",
            "server/hermes_ip12/media_library/",
            "server/hermes_ip12/knowledge/",
            "server/hermes_ip12/.agnes_key",
            "server/hermes_ip12/agnes_key.txt",
            "server/hermes_ip12/*cookies*.txt",
            "server/hermes_ip12/backups/",
            "server/hermes_ip12/nohup.out",
        ):
            self.assertIn(path, ignore)

        runbook = (ROOT / "deploy/生产环境清单与还原手册.md").read_text(
            encoding="utf-8"
        )
        self.assertIn('git archive "$HERMES_SHA"', runbook)
        self.assertIn("deploy/nginx-huangquechuanmei.conf", runbook)
        self.assertIn("hermes-last-backup", runbook)
        self.assertIn("systemctl restart hermes-ip12-preview.service", runbook)
        self.assertIn("rsync -a --delete", runbook)

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_markdown_guard_blocks_script_protocols_and_falls_back(self):
        for filename in ("index.html", "index_clean.html"):
            source = (HERMES / "templates" / filename).read_text(encoding="utf-8")
            guard = extract_js_function(source, "isSafeMarkdownUrl")
            script = guard + r'''
if (isSafeMarkdownUrl("javascript:alert(1)")) process.exit(1);
if (isSafeMarkdownUrl("data:text/html,<script>alert(1)</script>")) process.exit(2);
if (!isSafeMarkdownUrl("/report/1")) process.exit(3);
if (!isSafeMarkdownUrl("https://example.com/report")) process.exit(4);
'''
            result = subprocess.run(
                ["node", "-e", script], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, filename + result.stderr)

        classic = (HERMES / "templates/index_clean.html").read_text(encoding="utf-8")
        fallback = "\n".join(
            extract_js_function(classic, name)
            for name in ("renderMarkdown", "escHtml")
        )
        script = r'''
global.document={createElement:()=>({value:"",set textContent(v){this.value=String(v||"")},get innerHTML(){return this.value.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}})};
''' + fallback + r'''
const rendered=renderMarkdown("<img src=x onerror=alert(1)>\nnext");
if (!rendered.includes("&lt;img") || !rendered.includes("<br>")) process.exit(5);
'''
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(
    importlib.util.find_spec("flask") and importlib.util.find_spec("requests") and importlib.util.find_spec("pypdf"),
    "Hermes runtime dependencies are not installed",
)
class HermesIP12RuntimeTests(unittest.TestCase):
    def test_app_registers_and_core_storage_round_trip_works(self):
        script = r'''
import io
import base64
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import server
from server import _foundation_generation_active, _foundation_html, _foundation_source_messages, _validate_foundation_pdf, app, parse_coach_state_updates
import image_services
import media_library
import video_analyzer
import video_replica

server.current_account_id = lambda: "acct_a"
routes = {rule.rule for rule in app.url_map.iter_rules() if rule.endpoint != "static"}
assert len(routes) == 76, len(routes)

transitioned = parse_coach_state_updates(
    "好，我们进入模块2：人设塑造。",
    {"current_module": 1, "completed_modules": [], "module_step": 0},
)
assert transitioned["current_module"] == 2, transitioned
assert transitioned["completed_modules"] == [1], transitioned
foundation = parse_coach_state_updates(
    "✅ 模块 4 完成",
    {"current_module": 4, "completed_modules": [1, 2, 3], "module_step": 0},
)
assert foundation["current_module"] == 4, foundation
assert foundation["foundation_report"]["status"] == "generating", foundation
blocked_transition = parse_coach_state_updates(
    "✅ 模块 4 完成。接下来进入模块 5。",
    {"current_module": 4, "completed_modules": [1, 2, 3], "module_step": 0},
)
assert blocked_transition["current_module"] == 4, blocked_transition
assert blocked_transition["completed_modules"] == [1, 2, 3, 4], blocked_transition
revisited = parse_coach_state_updates(
    "✅ 模块 4 完成。接下来进入模块 5。",
    {"current_module": 4, "completed_modules": [1, 2, 3, 4], "module_step": 0,
     "foundation_report": {"status": "confirmed"}},
)
assert revisited["current_module"] == 5, revisited
assert revisited["foundation_report"]["status"] == "confirmed", revisited
assert 5 not in parse_coach_state_updates(
    "✅ 模块 5 完成",
    {"current_module": 4, "completed_modules": [1, 2, 3, 4], "module_step": 0,
     "foundation_report": {"status": "awaiting_confirmation"}},
)["completed_modules"]
assert parse_coach_state_updates(
    "本次诊断全部完成，正式结业",
    {"current_module": 1, "completed_modules": [], "module_step": 0},
)["completed_modules"] == []
source_messages = [
    {"role": "user", "content": "模块一资料"},
    {"role": "assistant", "content": "继续模块四"},
    {"role": "assistant", "content": "✅ 模块 4 完成"},
    {"role": "user", "content": "模块五资料"},
]
assert _foundation_source_messages({"messages": source_messages}) == source_messages[:3]
transition_messages = source_messages[:2] + [
    {"role": "assistant", "content": "接下来进入模块 5"},
    {"role": "user", "content": "模块五资料"},
]
assert _foundation_source_messages({"messages": transition_messages}) == transition_messages[:2]
assert _foundation_source_messages({
    "messages": source_messages,
    "coach_state": {"foundation_source_message_count": 2},
}) == source_messages[:2]
assert not _foundation_generation_active({"status": "generating", "started_at": "2099-01-01 00:00:00", "process_run_id": "old-process"})

def write_test_pdf(path, pages=8):
    stream = b"q\nQ\n%" + b"0" * 10000 + b"\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + b" ".join(f"{i} 0 R".encode() for i in range(3, 3 + pages)) + f"] /Count {pages} >>".encode(),
    ]
    objects.extend(
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {3 + pages} 0 R >>".encode()
        for _ in range(pages)
    )
    objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream")
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data)); data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data); data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(data)

valid_pdf = Path(os.environ["HERMES_DATA_DIR"]) / "valid.pdf"
write_test_pdf(valid_pdf)
assert _validate_foundation_pdf(valid_pdf) == 8
if shutil.which("pdfinfo"):
    assert subprocess.run(["pdfinfo", str(valid_pdf)], capture_output=True).returncode == 0
invalid_pdf = Path(os.environ["HERMES_DATA_DIR"]) / "invalid.pdf"
invalid_body = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n" + b"/Type /Page\n" * 8 + b"0" * 10000
invalid_pdf.write_bytes(invalid_body + b"\nxref\nthis is not a cross-reference table\ntrailer\n<< /Root 1 0 R /Size 10 >>\nstartxref\n" + str(len(invalid_body) + 1).encode() + b"\n%%EOF\n")
try:
    _validate_foundation_pdf(invalid_pdf)
    raise AssertionError("structurally invalid PDF was accepted")
except RuntimeError:
    pass
report_html = _foundation_html("""# 忽略的总标题
## 模块一｜定位诊断
### 核心关键词
#### 故事名称：从无到有
1. **实战**：有可验证经历。
| 场景 | 建议口径 |
| --- | --- |
| 账号封面 | 直接说结果 |
> 待本人确认
""")
assert "模块一｜定位诊断" in report_html
assert "<table>" in report_html and "账号封面" in report_html
assert "<blockquote>待本人确认</blockquote>" in report_html
assert "<h4>故事名称：从无到有</h4>" in report_html

client = app.test_client()
for path in ("/", "/classic", "/skills", "/analytics", "/images", "/videos",
             "/video-factory", "/pipeline", "/agnes-lab", "/team-workbench"):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)

created = client.post("/api/conversations").get_json()
cid = created["id"]
owned = client.get(f"/api/conversations/{cid}").get_json()
assert owned["id"] == cid and owned["owner_account_id"] == "acct_a"
assert client.get(f"/api/conversations/{cid}/reports").get_json() == {}
assert client.get(f"/api/conversations/{cid}/deliverables").get_json() == {}
server.current_account_id = lambda: "acct_b"
assert client.get(f"/api/conversations/{cid}").status_code == 404
assert client.get(f"/api/foundation-report/{cid}.pdf").status_code == 404
assert client.post("/api/foundation-report/generate", json={"conversation_id": cid}).status_code == 404
assert client.post("/api/foundation-report/confirm", json={"conversation_id": cid}).status_code == 404
assert client.get("/api/conversations").get_json() == []
server.current_account_id = lambda: "acct_a"
assert client.delete(f"/api/conversations/{cid}").get_json()["ok"] is True

foundation_cid = client.post("/api/conversations").get_json()["id"]
assert client.post("/api/foundation-report/confirm", json={"conversation_id": foundation_cid}).status_code == 409
assert client.post("/api/jump-module", json={"conversation_id": foundation_cid, "module": 5}).status_code == 409
assert client.post("/api/foundation-report/generate", json={"conversation_id": foundation_cid}).status_code == 409

gated = server.load_conversation(foundation_cid)
gated["coach_state"] = {"current_module": 4, "completed_modules": [1, 2, 3, 4],
                         "module_step": 0, "foundation_report": {"status": "generating"}}
server.save_conversation(foundation_cid, gated)
with patch.object(server, "call_ai") as gated_model:
    gated_reply = client.post("/api/chat-complete", json={"conversation_id": foundation_cid, "message": "继续"})
    assert gated_reply.status_code == 409, gated_reply.get_data(as_text=True)
    gated_model.assert_not_called()
assert client.post("/api/generate-report", json={"conversation_id": foundation_cid, "module": 5}).status_code == 409
assert client.post("/api/generate-deliverable", json={"conversation_id": foundation_cid, "module": 5}).status_code == 409

gated = server.load_conversation(foundation_cid)
gated["coach_state"] = {"current_module": 8, "completed_modules": list(range(1, 8)),
                         "module_step": 3, "foundation_report": {"status": "awaiting_confirmation"}}
server.save_conversation(foundation_cid, gated)
server.FOUNDATION_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
foundation_pdf = server.FOUNDATION_REPORTS_DIR / f"{foundation_cid}.pdf"
foundation_pdf.unlink(missing_ok=True)
assert client.get(f"/api/foundation-report/{foundation_cid}.pdf").status_code == 404
gated = server.load_conversation(foundation_cid)
assert gated["coach_state"]["foundation_report"]["status"] == "failed"
gated["coach_state"]["foundation_report"] = {"status": "awaiting_confirmation"}
server.save_conversation(foundation_cid, gated)
foundation_pdf.write_bytes(invalid_pdf.read_bytes())
assert client.get(f"/api/foundation-report/{foundation_cid}.pdf").status_code == 409
gated = server.load_conversation(foundation_cid)
assert gated["coach_state"]["foundation_report"]["status"] == "failed"
gated["coach_state"]["foundation_report"] = {"status": "awaiting_confirmation"}
server.save_conversation(foundation_cid, gated)
foundation_pdf.write_bytes(invalid_pdf.read_bytes())
assert client.post("/api/foundation-report/confirm", json={"conversation_id": foundation_cid}).status_code == 409
gated = server.load_conversation(foundation_cid)
assert gated["coach_state"]["foundation_report"]["status"] == "failed"
gated["coach_state"]["foundation_report"] = {"status": "awaiting_confirmation"}
server.save_conversation(foundation_cid, gated)
foundation_pdf.write_bytes(valid_pdf.read_bytes())
with patch.object(server, "call_ai") as report_model:
    duplicate = client.post("/api/foundation-report/generate", json={"conversation_id": foundation_cid})
    assert duplicate.status_code == 409, duplicate.get_data(as_text=True)
    report_model.assert_not_called()
download = client.get(f"/api/foundation-report/{foundation_cid}.pdf")
assert download.status_code == 200
assert download.headers["Cache-Control"] == "private, no-store"
confirmed = client.post("/api/foundation-report/confirm", json={"conversation_id": foundation_cid})
assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
assert confirmed.get_json()["state"]["current_module"] == 8
assert confirmed.get_json()["state"]["module_step"] == 3
assert client.post(
    "/api/chat",
    json={"conversation_id": "../../knowledge/visual_formulas", "message": "test"},
).status_code == 400

with patch.object(server, "call_ai") as chat_model:
    chat_model.return_value.json.return_value = {
        "choices": [{"message": {"content": "请先告诉我，你希望大家如何称呼你？"}}]
    }
    mini_cid = client.post("/api/conversations").get_json()["id"]
    mini_reply = client.post(
        "/api/chat-complete", json={"conversation_id": mini_cid, "message": "开始"}
    )
    assert mini_reply.status_code == 200, mini_reply.get_data(as_text=True)
    assert mini_reply.get_json()["assistant"]
server.current_account_id = lambda: "acct_b"
assert client.post(
    "/api/chat-complete", json={"conversation_id": mini_cid, "message": "越权"}
).status_code == 404
server.current_account_id = lambda: "acct_a"

uploaded = client.post(
    "/api/agnes/upload-image",
    data={"files": (io.BytesIO(b"test-image"), "test.png")},
    content_type="multipart/form-data",
    base_url="https://huangquechuanmei.com",
    headers={
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Prefix": "/workbench/ip12",
    },
).get_json()
assert uploaded["files"][0]["public_url"].startswith(
    "https://huangquechuanmei.com/workbench/ip12/media/agnes/images/"
), uploaded

media = client.post(
    "/api/media/upload",
    json={
        "keyword": "../../outside",
        "filename": "../../probe.png",
        "data": base64.b64encode(b"image").decode(),
    },
)
assert media.status_code == 200, media.get_data(as_text=True)
saved = Path(media.get_json()["path"]).resolve()
media_root = (Path(os.environ["HERMES_HOME"]).resolve() / "media_library").resolve()
assert saved.is_relative_to(media_root)
index = json.loads((media_root / "index.json").read_text())
assert all(Path(entry["file_path"]).resolve().is_relative_to(media_root)
           for entry in index["entries"].values())
assert client.post(
    "/api/media/upload",
    json={"filename": "../../probe.py", "data": base64.b64encode(b"bad").decode()},
).status_code == 400

pipeline_upload = client.post(
    "/api/pipeline-upload",
    data={"video": (io.BytesIO(b"video"), "../../clip.mp4")},
    content_type="multipart/form-data",
)
assert pipeline_upload.status_code == 200, pipeline_upload.get_data(as_text=True)
pipeline_path = Path(pipeline_upload.get_json()["path"]).resolve()
assert pipeline_path.is_relative_to((Path(os.environ["HERMES_DATA_DIR"]) / "uploads").resolve())
assert client.post(
    "/api/pipeline-upload",
    data={"video": (io.BytesIO(b"bad"), "../../clip.py")},
    content_type="multipart/form-data",
).status_code == 400
assert client.post(
    "/api/pipeline", json={"video_path": "/etc/passwd", "topic": "test"}
).status_code == 400

with patch.object(image_services.http_requests, "get") as proxy_get:
    blocked = client.get(
        "/api/proxy-image",
        query_string={"url": "http://127.0.0.1:3102/?pollinations"},
    )
    assert blocked.status_code == 400
    proxy_get.assert_not_called()

with patch.object(video_analyzer, "download_video") as video_download:
    blocked = client.post("/api/analyze-video", json={"url": "http://127.0.0.1/test"})
    assert blocked.status_code == 400
    video_download.assert_not_called()
assert client.post(
    "/api/analyze-video", json={"url": "https://example.com/video"}
).status_code == 400

class EmptyPexelsResponse:
    status_code = 200
    text = ""
    def json(self):
        return {"photos": []}

with patch.object(media_library.MediaLibrary, "search", return_value=[]), \
     patch.object(media_library.KnowledgeBase, "get_keyword_map", return_value=None), \
     patch.object(media_library, "google_search_images", return_value=[]), \
     patch("requests.get", return_value=EmptyPexelsResponse()) as pexels_get:
    assert media_library.get_best_image("test") == {"source": "none", "keyword": "test"}
    assert pexels_get.call_args.kwargs["headers"]["Authorization"] == "pexels-dummy"

with patch.object(video_replica, "PEXELS_KEY", ""), \
     patch("requests.get") as no_key_get:
    assert video_replica.search_pexels("test") is None
    no_key_get.assert_not_called()
with patch.object(video_replica, "PEXELS_KEY", "pexels-dummy"), \
     patch("requests.get", return_value=EmptyPexelsResponse()) as video_pexels_get:
    assert video_replica.search_pexels("test") is None
    assert video_pexels_get.call_args.kwargs["headers"]["Authorization"] == "pexels-dummy"
print("HERMES_RUNTIME_OK")
'''
        with tempfile.TemporaryDirectory() as data_dir:
            env = os.environ.copy()
            env.update(
                OPENAI_API_KEY="dummy",
                HERMES_HOME=data_dir,
                HERMES_DATA_DIR=data_dir,
                HERMES_ENABLE_INTERNAL_TOOLS="1",
                PEXELS_API_KEY="pexels-dummy",
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=HERMES,
                env=env,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HERMES_RUNTIME_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
