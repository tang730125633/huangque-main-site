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

    def test_complete_original_route_set_is_present(self):
        routes = set()
        pattern = re.compile(r'(?:@app\.route|app\.add_url_rule)\(\s*["\']([^"\']+)')
        for path in HERMES.glob("*.py"):
            routes.update(pattern.findall(path.read_text(encoding="utf-8")))

        self.assertEqual(len(routes), 71)
        self.assertTrue(
            {
                "/api/chat",
                "/api/generate-report",
                "/api/generate-deliverable",
                "/api/generate-image",
                "/api/generate-video",
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
    importlib.util.find_spec("flask") and importlib.util.find_spec("requests"),
    "Hermes runtime dependencies are not installed",
)
class HermesIP12RuntimeTests(unittest.TestCase):
    def test_app_registers_and_core_storage_round_trip_works(self):
        script = r'''
import io
import base64
import json
import os
from pathlib import Path
from unittest.mock import patch

from server import app
import image_services
import media_library
import video_analyzer
import video_replica

routes = {rule.rule for rule in app.url_map.iter_rules() if rule.endpoint != "static"}
assert len(routes) == 71, len(routes)

client = app.test_client()
for path in ("/", "/classic", "/skills", "/analytics", "/images", "/videos",
             "/video-factory", "/pipeline", "/agnes-lab", "/team-workbench"):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)

created = client.post("/api/conversations").get_json()
cid = created["id"]
assert client.get(f"/api/conversations/{cid}").get_json()["id"] == cid
assert client.get(f"/api/conversations/{cid}/reports").get_json() == {}
assert client.get(f"/api/conversations/{cid}/deliverables").get_json() == {}
assert client.delete(f"/api/conversations/{cid}").get_json()["ok"] is True
assert client.post(
    "/api/chat",
    json={"conversation_id": "../../knowledge/visual_formulas", "message": "test"},
).status_code == 400

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
