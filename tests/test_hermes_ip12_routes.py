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
        self.assertIn("当前产品只开放模块 1-6", prompt)
        self.assertIn("current_module 保持 6，不进入模块 7", prompt)
        self.assertNotIn("current_module = 7", prompt)

    def test_only_six_modules_are_open_in_both_web_views(self):
        for filename in ("index.html", "index_clean.html"):
            page = (HERMES / "templates" / filename).read_text(encoding="utf-8")
        self.assertIn("AVAILABLE_MODULE_COUNT", page)
        self.assertIn("尚未开发，敬请期待", page)
        self.assertIn("0/6", page)
        skills = (HERMES / "templates/skills.html").read_text(encoding="utf-8")
        videos = (HERMES / "templates/videos.html").read_text(encoding="utf-8")
        self.assertIn("s.m>6?'尚未开发，敬请期待'", skills)
        self.assertNotIn("fetch('/api/module8-video'", videos)
        self.assertIn("尚未开发，敬请期待", videos)

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
        self.assertLess(
            source.index("playwright.chromium.executable_path"),
            source.index('shutil.which("chromium")'),
        )
        self.assertIn("价值主张诊断表", source)
        self.assertIn("故事库（至少5个）", source)
        self.assertIn("内容资产使用表", source)
        self.assertIn("优化建议汇总", source)

    def test_service_security_boundary_is_registered(self):
        server = (HERMES / "server.py").read_text(encoding="utf-8")
        security = (HERMES / "security.py").read_text(encoding="utf-8")
        artifact_store = (HERMES / "artifact_store.py").read_text(encoding="utf-8")
        video_factory = (HERMES / "video_factory.py").read_text(encoding="utf-8")

        self.assertIn("register_security(app, DATA_DIR)", server)
        self.assertIn('HERMES_ENABLE_INTERNAL_TOOLS", "0"', server)
        self.assertIn('AUTH_BASE + "/api/auth/me"', security)
        self.assertIn('request.path == "/healthz"', security)
        self.assertIn("authentication service unavailable", security)
        self.assertIn("administrator permission required", security)
        self.assertIn("Hermes storage quota exceeded", security)
        self.assertIn("too many concurrent requests", security)
        self.assertIn("too many requests", security)
        self.assertIn('response.headers["X-Request-ID"]', security)
        self.assertIn('"duration_ms"', security)
        self.assertIn('"request_id"', security)
        self.assertIn("def atomic_write_bytes", artifact_store)
        self.assertIn("def atomic_append_bytes", artifact_store)
        self.assertIn("def video_work_dir", artifact_store)
        self.assertIn('LEGACY_ROLLBACK_DIRS = frozenset({"videos", "analyses", "uploads"})', artifact_store)
        self.assertIn("def _quota_paths():", artifact_store)
        self.assertIn('(?:ref_|replica_)?([0-9a-f]{10})', artifact_store)
        self.assertIn("owned_video_path(current_username(), filename)", video_factory)
        for filename in (
            "video_factory.py", "video_analyzer.py", "video_pipeline.py", "video_replica.py"
        ):
            source = (HERMES / filename).read_text(encoding="utf-8")
            self.assertIn("video_work_dir(", source, filename)
            self.assertIn("finalize_file(", source, filename)
        self.assertIn("if _is_metered(request.method)", security)
        media_library = (HERMES / "media_library.py").read_text(encoding="utf-8")
        video_analyzer = (HERMES / "video_analyzer.py").read_text(encoding="utf-8")
        self.assertIn("with storage_transaction():", media_library)
        self.assertIn('entry_id = f"{owner_id}_{new_asset_id()}"', media_library)

        runbook = (ROOT / "deploy" / "生产环境清单与还原手册.md").read_text(
            encoding="utf-8"
        )
        release_script = (ROOT / "deploy/hermes-ip12-release.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("http://127.0.0.1:3102/healthz", release_script)
        self.assertIn(
            "https://huangquechuanmei.com/workbench/ip12/healthz", release_script
        )
        self.assertIn("http://129.204.166.13:3101/healthz", release_script)
        self.assertNotIn("http://127.0.0.1:3102/ >/dev/null", release_script)
        self.assertEqual(
            video_analyzer.count('"--max-filesize", ANALYSIS_MAX_DOWNLOAD_ARG'), 2
        )
        self.assertIn("with reserve_capacity(ANALYSIS_MAX_DOWNLOAD_BYTES)", video_analyzer)
        agnes_routes = (HERMES / "agnes_routes.py").read_text(encoding="utf-8")
        team_routes = (HERMES / "team_workbench_routes.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("atomic_write_bytes", agnes_routes)
        self.assertIn("reserve_capacity", agnes_routes)
        self.assertIn("atomic_write_bytes", team_routes)
        self.assertIn("reserve_capacity", team_routes)

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
        self.assertNotIn("/api/module8-video", (HERMES / "templates/videos.html").read_text(encoding="utf-8"))
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
        self.assertIn("deploy/hermes-ip12-release.sh", runbook)
        self.assertIn("deploy/nginx-huangquechuanmei.conf", runbook)
        self.assertIn("hermes-last-backup", runbook)
        self.assertIn("deploy/hermes-ip12-release.sh", runbook)
        release_start = runbook.index("HERMES_STAGE=$(mktemp -d)")
        release_end = runbook.index("\n```", release_start)
        release = runbook[release_start:release_end]
        self.assertIn("scripts/migrate_hermes_artifacts.py", release)
        self.assertIn(
            'test -f "$HERMES_STAGE/scripts/migrate_hermes_artifacts.py"',
            release,
        )
        self.assertIn(
            'test -f "$HERMES_STAGE/deploy/hermes-ip12-release.sh"',
            release,
        )
        release_script = (ROOT / "deploy/hermes-ip12-release.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("trap rollback_release EXIT", release_script)
        self.assertIn("restore_file", release_script)
        self.assertIn("systemctl daemon-reload", release_script)
        self.assertIn("fail_if_requested rsync", release_script)
        self.assertIn("fail_if_requested pip", release_script)
        self.assertIn("fail_if_requested health", release_script)
        self.assertIn('DEPLOY_USER="${HERMES_DEPLOY_USER:-$(id -un)}"', release_script)
        self.assertIn('DEPLOY_GROUP="${HERMES_DEPLOY_GROUP:-$(id -gn)}"', release_script)
        self.assertIn(
            'install -d -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" -m 0700',
            release_script,
        )
        self.assertIn("Hermes rollback FAILED; manual recovery required", release_script)
        self.assertIn('exit "$ROLLBACK_FAILURE_EXIT"', release_script)
        self.assertLess(
            release_script.index('systemctl stop "$SERVICE"'),
            release_script.index("--dry-run"),
        )
        self.assertLess(
            release_script.index("--dry-run"),
            release_script.index('"$HERMES_RELEASE_DIR/server/hermes_ip12/"'),
        )
        self.assertLess(
            release_script.index("--dry-run"),
            release_script.rindex('systemctl restart "$SERVICE"'),
        )
        env_example = (ROOT / "deploy" / "hermes-ip12.env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("HERMES_LEGACY_OWNER=", env_example)
        self.assertIn("HERMES_DATA_QUOTA_MB=2048", env_example)
        self.assertIn("HERMES_DEPLOY_USER=ubuntu", env_example)
        self.assertIn("HERMES_DEPLOY_GROUP=ubuntu", env_example)

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
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import server
from server import _foundation_generation_active, _foundation_html, _foundation_source_messages, _render_foundation_pdf, _validate_foundation_pdf, app, parse_coach_state_updates
import security
import artifact_store
import image_services
import media_library
import video_analyzer
import video_factory
import video_pipeline
import video_replica
import video_vision

server.current_account_id = lambda: "acct_a"
security._validate_token = lambda token: {
    "admin-token": {"account_id": "acct_a", "username": "admin", "role": "admin"},
    "member-a-token": {"account_id": "acct_a", "username": "member-a", "role": "member"},
    "member-b-token": {"account_id": "acct_b", "username": "member-b", "role": "member"},
}.get(token)
security.RATE_REQUESTS = 1000
routes = {rule.rule for rule in app.url_map.iter_rules() if rule.endpoint != "static"}
assert len(routes) == 76, len(routes)
assert all(
    security._is_metered(method)
    for rule in app.url_map.iter_rules()
    for method in rule.methods
    if method in {"POST", "PUT", "PATCH", "DELETE"}
)

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
finished = parse_coach_state_updates(
    "✅ 模块 6 完成。接下来进入模块 7。",
    {"current_module": 6, "completed_modules": [1, 2, 3, 4, 5], "module_step": 0,
     "foundation_report": {"status": "confirmed"}},
)
assert finished["current_module"] == 6, finished
assert finished["completed_modules"] == [1, 2, 3, 4, 5, 6], finished
legacy = parse_coach_state_updates(
    "继续复盘",
    {"current_module": 8, "completed_modules": list(range(1, 8)), "module_step": 2,
     "foundation_report": {"status": "confirmed"}},
)
assert legacy["current_module"] == 6, legacy
assert legacy["completed_modules"] == [1, 2, 3, 4, 5, 6], legacy
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

render_root = Path(os.environ["HERMES_DATA_DIR"]) / "foundation-render"
render_root.mkdir()
render_calls = []
def fake_render(args, **kwargs):
    render_calls.append(args[0])
    html_text = Path(args[-1][7:]).read_text(encoding="utf-8")
    pdf_path = Path(next(item.split("=", 1)[1] for item in args if item.startswith("--print-to-pdf=")))
    write_test_pdf(pdf_path, 8 if "body{zoom:1.05}" in html_text else 7)
    return subprocess.CompletedProcess(args, 0)
with patch.object(server.subprocess, "run", side_effect=fake_render):
    fitted_pdf = _render_foundation_pdf("## 模块一", ["/fake/chromium"], render_root)
assert _validate_foundation_pdf(fitted_pdf) == 8
assert render_calls == ["/fake/chromium", "/fake/chromium"]

fallback_root = Path(os.environ["HERMES_DATA_DIR"]) / "foundation-fallback"
fallback_root.mkdir()
fallback_calls = []
def fake_fallback(args, **kwargs):
    fallback_calls.append(args[0])
    if args[0] == "/fake/playwright":
        raise subprocess.TimeoutExpired(args, 60)
    pdf_path = Path(next(item.split("=", 1)[1] for item in args if item.startswith("--print-to-pdf=")))
    write_test_pdf(pdf_path, 8)
    return subprocess.CompletedProcess(args, 0)
with patch.object(server.subprocess, "run", side_effect=fake_fallback):
    fallback_pdf = _render_foundation_pdf("## 模块一", ["/fake/playwright", "/fake/chromium"], fallback_root)
assert _validate_foundation_pdf(fallback_pdf) == 8
assert fallback_calls == ["/fake/playwright", "/fake/chromium"]

anonymous = app.test_client()
assert anonymous.get("/healthz").status_code == 200
assert anonymous.get("/").status_code == 401
client = app.test_client()
client.environ_base["HTTP_AUTHORIZATION"] = "Bearer admin-token"
for path in ("/", "/classic", "/skills", "/analytics", "/images", "/videos",
             "/video-factory", "/pipeline", "/agnes-lab", "/team-workbench"):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)

created_response = client.post(
    "/api/conversations", json={"title": "CLI 客户诊断"},
    headers={"X-Request-ID": "hermes_runtime_1234"},
)
assert created_response.headers["X-Request-ID"] == "hermes_runtime_1234"
created = created_response.get_json()
cid = created["id"]
audit_rows = [json.loads(line) for line in (Path(os.environ["HERMES_DATA_DIR"]) / "audit" / "security.jsonl").read_text().splitlines()]
created_audit = [row for row in audit_rows if row.get("request_id") == "hermes_runtime_1234"][-1]
assert created_audit["username"] == "admin"
assert created_audit["status"] == 200
assert created_audit["duration_ms"] >= 0
created_response.close()
assert security._active.get("admin", 0) == 0, security._active
owned = client.get(f"/api/conversations/{cid}").get_json()
assert owned["id"] == cid and owned["owner_account_id"] == "acct_a" and owned["title"] == "CLI 客户诊断"
assert client.post("/api/conversations", json={"unknown": True}).status_code == 400
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
assert client.post("/api/jump-module", json={"conversation_id": foundation_cid, "module": 7}).status_code == 409
assert client.post("/api/generate-report", json={"conversation_id": foundation_cid, "module": 7}).status_code == 409
assert client.post("/api/generate-deliverable", json={"conversation_id": foundation_cid, "module": 7}).status_code == 409
for coming_soon_path in ("/api/module7-images", "/api/module8-video", "/api/m9-funnel", "/api/m11-sales", "/api/m12-calendar"):
    assert client.post(coming_soon_path, json={}).status_code == 409, coming_soon_path

gated = server.load_conversation(foundation_cid)
gated["coach_state"] = {"current_module": 8, "completed_modules": list(range(1, 8)),
                         "module_step": 3, "foundation_report": {"status": "awaiting_confirmation"}}
server.save_conversation(foundation_cid, gated)
legacy_detail = client.get(f"/api/conversations/{foundation_cid}").get_json()["coach_state"]
assert legacy_detail["current_module"] == 6, legacy_detail
assert legacy_detail["completed_modules"] == [1, 2, 3, 4, 5, 6], legacy_detail
assert legacy_detail["module_step"] == 0, legacy_detail
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
assert confirmed.get_json()["state"]["current_module"] == 6
assert confirmed.get_json()["state"]["module_step"] == 0

normal_confirm_cid = client.post("/api/conversations").get_json()["id"]
normal_confirm = server.load_conversation(normal_confirm_cid)
normal_confirm["coach_state"] = {"current_module": 4, "completed_modules": [1, 2, 3, 4],
                                   "module_step": 4, "foundation_report": {"status": "awaiting_confirmation"}}
server.save_conversation(normal_confirm_cid, normal_confirm)
(server.FOUNDATION_REPORTS_DIR / f"{normal_confirm_cid}.pdf").write_bytes(valid_pdf.read_bytes())
confirmed = client.post("/api/foundation-report/confirm", json={"conversation_id": normal_confirm_cid})
assert confirmed.get_json()["state"]["current_module"] == 5
assert confirmed.get_json()["state"]["module_step"] == 0

owned_video = artifact_store.video_path("admin", "0123456789.mp4")
owned_video.parent.mkdir(parents=True, exist_ok=True)
owned_video.write_bytes(b"video")
assert client.get(
    "/api/video-file/0123456789.mp4",
    headers={"Authorization": "Bearer admin-token"},
).status_code == 200
assert client.get(
    "/api/video-file/0123456789.mp4",
    headers={"Authorization": "Bearer member-a-token"},
).status_code == 404
assert client.get(
    "/api/video-file/../../0123456789.mp4",
    headers={"Authorization": "Bearer admin-token"},
).status_code == 404

security._rate_hits.clear()
original_rate = security.RATE_REQUESTS
security.RATE_REQUESTS = 1
assert client.post("/api/humanize", json={"text": ""}).status_code == 400
assert client.post("/api/humanize", json={"text": ""}).status_code == 429
security.RATE_REQUESTS = original_rate
security._rate_hits.clear()

security._active["admin"] = security.USER_CONCURRENCY
assert client.post("/api/humanize", json={"text": ""}).status_code == 429
security._active.clear()

original_quota = artifact_store.DATA_QUOTA_BYTES
artifact_store.DATA_QUOTA_BYTES = 1
assert client.post("/api/media/upload", json={"data": "AAAA"}).status_code == 507
artifact_store.DATA_QUOTA_BYTES = original_quota
quota_first = artifact_store.media_path("admin", artifact_store.new_asset_id(), ".bin")
quota_second = artifact_store.media_path("admin", artifact_store.new_asset_id(), ".bin")
artifact_store.DATA_QUOTA_BYTES = artifact_store.directory_size() + 5
artifact_store.atomic_write_bytes(quota_first, b"1234")
try:
    artifact_store.atomic_write_bytes(quota_second, b"5678")
    raise AssertionError("second quota write should fail")
except artifact_store.StorageQuotaExceeded:
    pass
assert quota_first.exists() and not quota_second.exists()
artifact_store.DATA_QUOTA_BYTES = original_quota

# Rollback copies retained in the old flat directories are not counted twice.
canonical_size = artifact_store.directory_size()
legacy_video = Path(os.environ["HERMES_DATA_DIR"]) / "videos" / "legacy.mp4"
legacy_video.parent.mkdir(parents=True, exist_ok=True)
legacy_video.write_bytes(b"legacy" * 10000)
assert artifact_store.directory_size() == canonical_size

# Moving a retained legacy file into canonical storage still requires capacity.
legacy_move = legacy_video.with_name("legacy-move.bin")
legacy_move.write_bytes(b"0123456789")
legacy_destination = artifact_store.media_path(
    "admin", artifact_store.new_asset_id(), ".bin"
)
artifact_store.DATA_QUOTA_BYTES = artifact_store.directory_size() + 5
try:
    artifact_store.finalize_file(legacy_move, legacy_destination)
    raise AssertionError("legacy-to-canonical move should enforce quota")
except artifact_store.StorageQuotaExceeded:
    pass
assert legacy_move.exists() and not legacy_destination.exists()
artifact_store.DATA_QUOTA_BYTES = original_quota

# Cross-filesystem finalize falls back to a target-side atomic copy.
external_root = Path(os.environ["HERMES_DATA_DIR"]).parent
cross_source = external_root / f"hermes-cross-{os.getpid()}.bin"
cross_destination = artifact_store.media_path(
    "admin", artifact_store.new_asset_id(), ".bin"
)
cross_content = b"cross-filesystem-content"
cross_source.write_bytes(cross_content)
real_replace = artifact_store.os.replace
replace_calls = []

def replace_cross_device_once(source, destination):
    replace_calls.append((Path(source), Path(destination)))
    if len(replace_calls) == 1:
        raise OSError(errno.EXDEV, "cross-device link")
    return real_replace(source, destination)

with patch.object(
    artifact_store.os, "replace", side_effect=replace_cross_device_once
):
    artifact_store.finalize_file(cross_source, cross_destination)
assert not cross_source.exists()
assert cross_destination.read_bytes() == cross_content
assert hashlib.sha256(cross_destination.read_bytes()).digest() == hashlib.sha256(
    cross_content
).digest()
assert not list(cross_destination.parent.glob(f".{cross_destination.name}.*.tmp"))

# Copy interruption keeps the source and removes target-side temporary files.
interrupted_source = external_root / f"hermes-interrupted-{os.getpid()}.bin"
interrupted_destination = artifact_store.media_path(
    "admin", artifact_store.new_asset_id(), ".bin"
)
interrupted_source.write_bytes(b"complete-source")

def interrupt_copy(source, destination):
    Path(destination).write_bytes(b"partial")
    raise OSError(errno.EIO, "copy interrupted")

with patch.object(
    artifact_store.os, "replace", side_effect=OSError(errno.EXDEV, "cross-device link")
), patch.object(artifact_store.shutil, "copy2", side_effect=interrupt_copy):
    try:
        artifact_store.finalize_file(interrupted_source, interrupted_destination)
        raise AssertionError("interrupted copy should fail")
    except OSError as exc:
        assert exc.errno == errno.EIO
assert interrupted_source.exists()
assert not interrupted_destination.exists()
assert not list(
    interrupted_destination.parent.glob(f".{interrupted_destination.name}.*.tmp")
)
interrupted_source.unlink()

# Non-cross-device errors fail closed and never enter the copy fallback.
closed_source = external_root / f"hermes-closed-{os.getpid()}.bin"
closed_destination = artifact_store.media_path(
    "admin", artifact_store.new_asset_id(), ".bin"
)
closed_source.write_bytes(b"closed")
with patch.object(
    artifact_store.os, "replace", side_effect=PermissionError(errno.EACCES, "denied")
), patch.object(artifact_store.shutil, "copy2") as forbidden_copy:
    try:
        artifact_store.finalize_file(closed_source, closed_destination)
        raise AssertionError("permission error should fail")
    except PermissionError:
        pass
forbidden_copy.assert_not_called()
assert closed_source.exists() and not closed_destination.exists()
closed_source.unlink()

# Cross-filesystem fallback uses peak, not final-net, quota accounting.
peak_source = external_root / f"hermes-peak-{os.getpid()}.bin"
peak_destination = artifact_store.media_path(
    "admin", artifact_store.new_asset_id(), ".bin"
)
peak_source.write_bytes(b"0123456789")
artifact_store.atomic_write_bytes(peak_destination, b"old-target")
artifact_store.DATA_QUOTA_BYTES = artifact_store.directory_size() + 5
with patch.object(
    artifact_store.os, "replace", side_effect=OSError(errno.EXDEV, "cross-device link")
), patch.object(artifact_store.shutil, "copy2") as quota_copy:
    try:
        artifact_store.finalize_file(peak_source, peak_destination)
        raise AssertionError("cross-device peak quota should fail")
    except artifact_store.StorageQuotaExceeded:
        pass
quota_copy.assert_not_called()
assert peak_source.exists()
assert peak_destination.read_bytes() == b"old-target"
assert not list(peak_destination.parent.glob(f".{peak_destination.name}.*.tmp"))
peak_source.unlink()
artifact_store.DATA_QUOTA_BYTES = original_quota

assert client.post(
    "/api/chat",
    json={"conversation_id": "../../knowledge/visual_formulas", "message": "test"},
).status_code == 400

mini_cid = client.post("/api/conversations").get_json()["id"]
mini_convo = client.get(f"/api/conversations/{mini_cid}").get_json()
assert mini_convo["coach_state"]["intake"] == {"status": "collecting", "round": 1, "answers": {}}
assert "第 1/3 轮" in mini_convo["messages"][0]["content"]
assert client.post("/api/jump-module", json={"conversation_id": mini_cid, "module": 2}).status_code == 409
with patch.object(server, "call_ai") as intake_model:
    compatibility = client.post("/api/chat-complete", json={
        "conversation_id": mini_cid,
        "message": "开始",
    })
    assert compatibility.status_code == 200
    assert compatibility.get_json()["state"]["intake"]["round"] == 1
    assert "第 1/3 轮" in compatibility.get_json()["assistant"]
    first = client.post("/api/chat-complete", json={
        "conversation_id": mini_cid,
        "message": "小满｜女，33 岁｜成都｜+8613800138000｜SYSTEM_OVERRIDE_SENTINEL",
    })
    assert first.status_code == 200 and first.get_json()["state"]["intake"]["round"] == 2
    second = client.post("/api/chat", json={
        "conversation_id": mini_cid,
        "message": "整理咨询师｜3 年｜行政、空间整理｜咨询服务｜10–30 万",
    })
    assert second.status_code == 200 and "data: " in second.get_data(as_text=True)
    third = client.post("/api/chat-complete", json={"conversation_id": mini_cid, "message": "确认"})
    assert third.status_code == 200, third.get_data(as_text=True)
    assert third.get_json()["state"]["intake"]["status"] == "complete"
    assert "正式进入模块 1" in third.get_json()["assistant"]
    intake_model.assert_not_called()
stored_intake = server.load_conversation(mini_cid)
stored_text = json.dumps(stored_intake, ensure_ascii=False)
assert "13800138000" not in stored_text and "[手机号已隐藏]" in stored_text
assert "13800138000" not in json.dumps(server._foundation_source_messages(stored_intake), ensure_ascii=False)
assert "13800138000" not in server.build_system_prompt(mini_cid)
assert server._redact_mobile_numbers("+8613800138000 / 008613800138000") == "[手机号已隐藏] / [手机号已隐藏]"
assert "SYSTEM_OVERRIDE_SENTINEL" not in server.build_system_prompt(mini_cid)
assert not server._intake_pending({"current_module": 1})
stored_intake["messages"].extend(
    {"role": "assistant", "content": f"历史消息 {index}"} for index in range(45)
)
server.save_conversation(mini_cid, stored_intake)
with patch.object(server, "call_ai") as chat_model:
    chat_model.return_value.json.return_value = {
        "choices": [{"message": {"content": "请讲一段对你影响最大的关键经历。"}}]
    }
    module_reply = client.post(
        "/api/chat-complete", json={"conversation_id": mini_cid, "message": "我曾经重新选择职业方向。"}
    )
    assert module_reply.status_code == 200, module_reply.get_data(as_text=True)
    chat_model.assert_called_once()
    model_messages = chat_model.call_args.args[0]
    assert "SYSTEM_OVERRIDE_SENTINEL" not in model_messages[0]["content"]
    intake_contexts = [message for message in model_messages if message["role"] == "user" and "此前确认的基础资料" in message["content"]]
    assert len(intake_contexts) == 1 and "SYSTEM_OVERRIDE_SENTINEL" in intake_contexts[0]["content"]
    assert "13800138000" not in json.dumps(model_messages, ensure_ascii=False)
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

# Active internal-tool directories count toward quota. A successful upload must
# increase used space, and the next upload must be rejected on cumulative use.
original_quota = artifact_store.DATA_QUOTA_BYTES
agnes_images = Path(os.environ["HERMES_DATA_DIR"]) / "agnes_lab" / "images"
agnes_before_files = set(agnes_images.iterdir())
agnes_before_bytes = artifact_store.directory_size()
artifact_store.DATA_QUOTA_BYTES = agnes_before_bytes + 4096
agnes_first = client.post(
    "/api/agnes/upload-image",
    data={"files": (io.BytesIO(b"first-agnes"), "first.png")},
    content_type="multipart/form-data",
)
assert agnes_first.status_code == 200, agnes_first.get_data(as_text=True)
agnes_after_bytes = artifact_store.directory_size()
assert agnes_after_bytes > agnes_before_bytes
agnes_after_files = set(agnes_images.iterdir())
assert len(agnes_after_files - agnes_before_files) == 1
artifact_store.DATA_QUOTA_BYTES = agnes_after_bytes + 1
agnes_second = client.post(
    "/api/agnes/upload-image",
    data={"files": (io.BytesIO(b"second-agnes"), "second.png")},
    content_type="multipart/form-data",
)
assert agnes_second.status_code == 507, agnes_second.get_data(as_text=True)
assert set(agnes_images.iterdir()) == agnes_after_files

team_uploads = (
    Path(os.environ["HERMES_DATA_DIR"]) / "team_workbench" / "uploads" / "images"
)
team_before_files = set(team_uploads.iterdir())
team_before_bytes = artifact_store.directory_size()
artifact_store.DATA_QUOTA_BYTES = team_before_bytes + 4096
team_first = client.post(
    "/api/team-workbench/upload",
    data={"files": (io.BytesIO(b"first-team"), "first.png")},
    content_type="multipart/form-data",
)
assert team_first.status_code == 200, team_first.get_data(as_text=True)
team_after_bytes = artifact_store.directory_size()
assert team_after_bytes > team_before_bytes
team_after_files = set(team_uploads.iterdir())
assert len(team_after_files - team_before_files) == 1
artifact_store.DATA_QUOTA_BYTES = team_after_bytes + 1
team_second = client.post(
    "/api/team-workbench/upload",
    data={"files": (io.BytesIO(b"second-team"), "second.png")},
    content_type="multipart/form-data",
)
assert team_second.status_code == 507, team_second.get_data(as_text=True)
assert set(team_uploads.iterdir()) == team_after_files
assert not list(Path(os.environ["HERMES_DATA_DIR"]).rglob("*.tmp"))
artifact_store.DATA_QUOTA_BYTES = original_quota

media = client.post(
    "/api/media/upload",
    json={
        "keyword": "../../outside",
        "filename": "../../probe.png",
        "data": base64.b64encode(b"image").decode(),
    },
)
assert media.status_code == 200, media.get_data(as_text=True)
media_root = Path(os.environ["HERMES_DATA_DIR"]).resolve()
index = json.loads((media_root / "media_library" / "index.json").read_text())
saved = Path(index["entries"][media.get_json()["id"]]["file_path"]).resolve()
assert saved.is_relative_to(media_root)
assert all(Path(entry["file_path"]).resolve().is_relative_to(media_root)
           for entry in index["entries"].values())
assert all(entry["owner_username"] == "admin" for entry in index["entries"].values())
assert client.get(
    "/api/media/search?q=outside",
    headers={"Authorization": "Bearer member-b-token"},
).get_json()["results"] == []

# Same-second/same-keyword uploads must retain both owners without keyword leakage.
source_a = media_root / "same-a.bin"
source_b = media_root / "same-b.bin"
source_a.write_bytes(b"owner-a")
source_b.write_bytes(b"owner-b")
with patch.object(media_library.time, "time", return_value=1234567890):
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(
            media_library.MediaLibrary.add,
            "private-campaign", str(source_a),
            owner_username="member-a",
        )
        future_b = pool.submit(
            media_library.MediaLibrary.add,
            "private-campaign", str(source_b),
            owner_username="member-b",
        )
        media_id_a, media_id_b = future_a.result(), future_b.result()
assert media_id_a != media_id_b
index = json.loads((media_root / "media_library" / "index.json").read_text())
assert index["entries"][media_id_a]["owner_username"] == "member-a"
assert index["entries"][media_id_b]["owner_username"] == "member-b"
assert media_library.MediaLibrary.search(
    "private-campaign", owner_username="member-a"
) == [index["entries"][media_id_a]]
assert media_library.MediaLibrary.search(
    "private-campaign", owner_username="member-b"
) == [index["entries"][media_id_b]]
admin_stats = media_library.MediaLibrary.stats(owner_username="admin")
member_a_stats = media_library.MediaLibrary.stats(owner_username="member-a")
assert "private-campaign" not in admin_stats["keywords"]
assert member_a_stats["total_files"] == 1
assert member_a_stats["keywords"] == ["private-campaign"]

# The same transaction lock must also protect independent worker processes.
process_sources = []
processes = []
child_code = (
    "import sys;"
    "from media_library import MediaLibrary;"
    "MediaLibrary.add(sys.argv[1],sys.argv[2],owner_username=sys.argv[3])"
)
for i in range(4):
    source = media_root / f"process-{i}.bin"
    source.write_bytes(f"process-{i}".encode())
    process_sources.append(source)
    processes.append(subprocess.Popen(
        [sys.executable, "-c", child_code, "process-private", str(source), f"process-{i}"],
        cwd=os.getcwd(),
        env=os.environ.copy(),
    ))
assert all(process.wait(timeout=30) == 0 for process in processes)
index = json.loads((media_root / "media_library" / "index.json").read_text())
assert sum(
    entry.get("keyword") == "process-private"
    for entry in index["entries"].values()
) == 4

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
pipeline_upload_id = pipeline_upload.get_json()["upload_id"]
pipeline_path = artifact_store.find_upload("admin", pipeline_upload_id)
assert pipeline_path.is_relative_to((Path(os.environ["HERMES_DATA_DIR"]) / "users").resolve())
assert client.post(
    "/api/pipeline",
    json={"upload_id": pipeline_upload_id, "topic": "test"},
    headers={"Authorization": "Bearer member-b-token"},
).status_code == 400
assert client.post(
    "/api/pipeline-upload",
    data={"video": (io.BytesIO(b"bad"), "../../clip.py")},
    content_type="multipart/form-data",
).status_code == 400
assert client.post(
    "/api/pipeline", json={"video_path": "/etc/passwd", "topic": "test"}
).status_code == 400

def fake_video_file(work_dir, name="output.mp4"):
    path = Path(work_dir) / name
    path.write_bytes(b"generated-video")
    return str(path)

def assert_owned_video(response):
    assert response.status_code == 200, response.get_data(as_text=True)
    url = response.get_json()["video_url"]
    owner_response = client.get(url)
    assert owner_response.status_code == 200, (url, owner_response.status_code)
    other_response = client.get(
        url, headers={"Authorization": "Bearer member-b-token"}
    )
    assert other_response.status_code == 404, (url, other_response.status_code)

fake_script = {
    "title": "test", "narration_full": "test",
    "scenes": [{"narration": "test", "visual": "test"}],
}
with patch.object(video_factory, "generate_script", return_value=fake_script), \
     patch.object(video_factory, "generate_all_images", side_effect=lambda scenes, work_dir: scenes), \
     patch.object(video_factory, "generate_tts_pro", return_value="audio"), \
     patch.object(video_factory, "generate_subtitles", return_value="subtitle"), \
     patch.object(video_factory, "compose_video_pro", side_effect=lambda *args: fake_video_file(args[-1])):
    assert_owned_video(client.post("/api/generate-video", json={"topic": "test"}))

analysis_id, analysis_root = artifact_store.analysis_dir("admin")
analysis_root.mkdir(parents=True, exist_ok=True)
(analysis_root / "result.json").write_text(json.dumps({
    "analysis_id": analysis_id, "owner_username": "admin",
    "analysis": "analysis", "transcript": "transcript",
}))
assert client.post(
    "/api/generate-from-analysis",
    json={"analysis_id": analysis_id, "topic": "test"},
    headers={"Authorization": "Bearer member-b-token"},
).status_code == 404
with patch.object(video_analyzer, "generate_script_with_reference", return_value=fake_script), \
     patch.object(video_factory, "generate_all_images", side_effect=lambda scenes, work_dir: scenes), \
     patch.object(video_factory, "generate_tts_pro", return_value="audio"), \
     patch.object(video_factory, "generate_subtitles", return_value="subtitle"), \
     patch.object(video_factory, "compose_video_pro", side_effect=lambda *args: fake_video_file(args[-1])):
    assert_owned_video(client.post(
        "/api/generate-from-analysis",
        json={"analysis_id": analysis_id, "topic": "test"},
    ))

with patch.object(video_pipeline, "transcribe_video", return_value={"full_text": "test"}), \
     patch.object(video_pipeline, "optimize", return_value={
         "title": "test", "scenes": [], "narration_full": "test"
     }), \
     patch.object(video_pipeline, "generate_videos", return_value=[]), \
     patch.object(video_pipeline, "generate_tts", return_value="audio"), \
     patch.object(video_pipeline, "compose_video", side_effect=lambda *args: fake_video_file(args[-1])), \
     patch.object(video_vision, "analyze_video_visual", return_value={"frames_analyzed": 1}), \
     patch.object(video_pipeline.KnowledgeBase, "add_formula"):
    assert_owned_video(client.post(
        "/api/pipeline",
        json={"upload_id": pipeline_upload_id, "topic": "test"},
    ))

with patch.object(video_replica, "replicate", return_value=[]), \
     patch.object(video_replica, "compose_final", side_effect=lambda clips, text, work_dir: fake_video_file(work_dir)):
    assert_owned_video(client.post(
        "/api/replica",
        json={"topic": "test", "segments": [{"text": "test"}]},
    ))

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

# Low quota must reject before any downloader side effect or analysis directory.
original_analysis_limit = video_analyzer.ANALYSIS_MAX_DOWNLOAD_BYTES
original_quota = artifact_store.DATA_QUOTA_BYTES
video_analyzer.ANALYSIS_MAX_DOWNLOAD_BYTES = 400
analyses_root = artifact_store.user_dir("admin", "analyses")
before_analyses = {path.name for path in analyses_root.iterdir()}
artifact_store.DATA_QUOTA_BYTES = artifact_store.directory_size() + 399
with patch.object(video_analyzer, "is_public_video_url", return_value=True), \
     patch.object(video_analyzer, "download_video") as quota_download:
    quota_response = client.post(
        "/api/analyze-video",
        json={"url": "https://douyin.com/video/quota"},
    )
assert quota_response.status_code == 507, quota_response.get_data(as_text=True)
quota_download.assert_not_called()
assert {path.name for path in analyses_root.iterdir()} == before_analyses

# Two analyses competing for the final quota cannot both reserve capacity.
analysis_started = threading.Event()
analysis_release = threading.Event()
artifact_store.DATA_QUOTA_BYTES = artifact_store.directory_size() + 700
before_analyses = {path.name for path in analyses_root.iterdir()}

def fake_analysis_download(url, work_dir):
    analysis_started.set()
    assert analysis_release.wait(timeout=10)
    path = Path(work_dir) / "source.mp4"
    path.write_bytes(b"analysis-video")
    return str(path)

def run_analysis():
    thread_client = app.test_client()
    return thread_client.post(
        "/api/analyze-video",
        json={"url": "https://douyin.com/video/test"},
        headers={"Authorization": "Bearer admin-token"},
    )

analysis_exdev = []
def replace_analysis_cross_device(source, destination):
    source_path = Path(source)
    destination_path = Path(destination)
    if (
        not analysis_exdev
        and source_path.name == "source.mp4"
        and source_path.parent.name.startswith("hermes-analysis-")
        and destination_path.name == "source.mp4"
    ):
        analysis_exdev.append((source_path, destination_path))
        raise OSError(errno.EXDEV, "cross-device link")
    return real_replace(source, destination)

with patch.object(video_analyzer, "is_public_video_url", return_value=True), \
     patch.object(video_analyzer, "download_video", side_effect=fake_analysis_download), \
     patch.object(video_analyzer, "transcribe_video", return_value="transcript"), \
     patch.object(video_analyzer, "analyze_transcript", return_value="analysis"), \
     patch.object(
         artifact_store.os, "replace", side_effect=replace_analysis_cross_device
     ):
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(run_analysis)
        assert analysis_started.wait(timeout=10)
        second = run_analysis()
        assert second.status_code == 507, second.get_data(as_text=True)
        analysis_release.set()
        first = first_future.result(timeout=10)
assert first.status_code == 200, first.get_data(as_text=True)
assert len(analysis_exdev) == 1
after_analyses = {path.name for path in analyses_root.iterdir()}
assert len(after_analyses - before_analyses) == 1
assert artifact_store.directory_size() <= artifact_store.DATA_QUOTA_BYTES
assert json.loads(artifact_store.RESERVATIONS_FILE.read_text()) == {}
video_analyzer.ANALYSIS_MAX_DOWNLOAD_BYTES = original_analysis_limit
artifact_store.DATA_QUOTA_BYTES = original_quota

class EmptyPexelsResponse:
    status_code = 200
    text = ""
    def json(self):
        return {"photos": []}

with patch.object(media_library.MediaLibrary, "_owner", return_value="admin"), \
     patch.object(media_library.MediaLibrary, "search", return_value=[]), \
     patch.object(media_library.KnowledgeBase, "get_keyword_map", return_value=None), \
     patch.object(media_library, "google_search_images", return_value=[]), \
     patch("requests.get", return_value=EmptyPexelsResponse()) as pexels_get:
    assert media_library.get_best_image("test") == {"source": "none", "keyword": "test"}
    assert pexels_get.call_args.kwargs["headers"]["Authorization"] == "pexels-dummy"

class ImageResponse:
    status_code = 200
    text = ""
    content = b"image-bytes" * 600
    def __init__(self, payload=None):
        self.payload = payload or {}
    def json(self):
        return self.payload

pexels_response = ImageResponse({
    "photos": [{
        "src": {"large": "https://img.example/pexels.jpg"},
        "photographer": "Pexels Owner",
    }],
})
with patch.object(media_library.MediaLibrary, "_owner", return_value="admin"), \
     patch.object(media_library.MediaLibrary, "search", return_value=[]), \
     patch.object(media_library.KnowledgeBase, "get_keyword_map", return_value=None), \
     patch("requests.get", side_effect=[pexels_response, ImageResponse()]):
    result = media_library.get_best_image("pexels-owned")
assert result["source"] == "pexels", result
pexels_entries = media_library.MediaLibrary._load()["entries"].values()
pexels_entry = next(entry for entry in pexels_entries if entry["keyword"] == "pexels-owned")
assert pexels_entry["owner_username"] == "admin", pexels_entry
assert Path(pexels_entry["file_path"]).is_relative_to(
    artifact_store.user_dir("admin", "media")
), pexels_entry

with patch.object(media_library.MediaLibrary, "_owner", return_value="admin"), \
     patch.object(media_library.MediaLibrary, "search", return_value=[]), \
     patch.object(media_library.KnowledgeBase, "get_keyword_map", return_value=None), \
     patch.object(media_library, "PEXELS_KEY", ""), \
     patch.object(media_library, "google_search_images", return_value=[{
         "url": "https://img.example/google.jpg",
         "title": "Google Owner",
     }]), \
     patch("requests.get", return_value=ImageResponse()):
    result = media_library.get_best_image("google-owned")
assert result["source"] == "google", result
google_entries = media_library.MediaLibrary._load()["entries"].values()
google_entry = next(entry for entry in google_entries if entry["keyword"] == "google-owned")
assert google_entry["owner_username"] == "admin", google_entry
assert Path(google_entry["file_path"]).is_relative_to(
    artifact_store.user_dir("admin", "media")
), google_entry

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
